"""索引（解析結果のキャッシュ）の蓄積動作。

解析対象をリポジトリ内に固定で持たず、実行のたびに索引へ積み上げていく
方式なので、「前に解析したものが消えない」ことが要になる。
実際の解析は走らせず、`prepare` / `analyze_prepared` を差し替えて検証する。
"""

from __future__ import annotations

import json

import pytest

from analyzer import analyze


def fake_analysis(repo: str, generated_at: str = "2026-01-01T00:00:00Z") -> dict:
    return {
        "schema_version": 1,
        "generated_at": generated_at,
        "source": {"repo": repo, "forks_scanned": [], "git_version": "2.43.0"},
        "integration_lines": [{"id": "main", "pr_count": 1}],
        "actions": [],
        "pull_requests": [],
        "interference": {},
        "orders": {},
        "warnings": [],
        "stats": {
            "prs_total": 1,
            "prs_analyzed": 1,
            "prs_skipped": 0,
            "conflict_pairs": 0,
            "base_conflicts": 0,
            "duration_sec": 0.1,
        },
    }


class FakePrepared:
    """`Prepared` の代役。`analyze_prepared` が読む分だけ持つ。"""

    def __init__(self, repo: str, lines: list[str]):
        self.target = repo
        self.line_names = list(lines)

    def discard(self) -> None:
        pass


def stub_pipeline(monkeypatch, *, fail: set[str] = frozenset(), fail_at: str = "analyze"):
    """準備と解析を差し替えて、解析されたリポジトリを記録する。

    準備と解析は別フェーズになっており（準備は先読みで並列に走る）、
    **どちらで失敗しても他のリポジトリが巻き込まれない**ことを確かめたいので、
    失敗させる側を選べるようにしてある。
    """
    calls: list[tuple[str, list[str]]] = []

    def _prepare(repo, lines, **kw):
        if fail_at == "prepare" and repo in fail:
            raise RuntimeError("準備に失敗")
        return FakePrepared(repo, lines)

    def _analyze(prepared, **kw):
        if fail_at == "analyze" and prepared.target in fail:
            raise RuntimeError("解析に失敗")
        calls.append((prepared.target, prepared.line_names))
        return fake_analysis(prepared.target)

    monkeypatch.setattr(analyze, "prepare", _prepare)
    monkeypatch.setattr(analyze, "analyze_prepared", _analyze)
    return calls


@pytest.fixture
def stub_run(monkeypatch):
    """実際の解析を差し替えて、呼ばれたリポジトリを記録する。"""
    return stub_pipeline(monkeypatch)


def index_of(outdir) -> dict:
    return json.loads((outdir / "index.json").read_text())


class TestAccumulation:
    def test_first_entry_creates_index(self, tmp_path, stub_run):
        analyze.analyze_and_cache(
            [{"repo": "owner/one", "lines": ["main"]}], tmp_path, verbose=False
        )
        idx = index_of(tmp_path)
        assert [e["repo"] for e in idx["analyses"]] == ["owner/one"]
        assert (tmp_path / "owner-one.json").exists()

    def test_second_entry_keeps_the_first(self, tmp_path, stub_run):
        """新しい対象を足しても、前の結果は索引から消えない。"""
        analyze.analyze_and_cache([{"repo": "owner/one", "lines": ["main"]}], tmp_path, verbose=False)
        analyze.analyze_and_cache([{"repo": "owner/two", "lines": ["main"]}], tmp_path, verbose=False)
        assert [e["repo"] for e in index_of(tmp_path)["analyses"]] == ["owner/one", "owner/two"]
        assert (tmp_path / "owner-one.json").exists()
        assert (tmp_path / "owner-two.json").exists()

    def test_reanalysis_updates_in_place(self, tmp_path, stub_run):
        """同じリポジトリを解析し直しても重複しない。"""
        for _ in range(3):
            analyze.analyze_and_cache(
                [{"repo": "owner/one", "lines": ["main"]}], tmp_path, verbose=False
            )
        assert len(index_of(tmp_path)["analyses"]) == 1

    def test_entries_are_sorted_by_repo_name(self, tmp_path, stub_run):
        """並びは辞書順に固定。実行順で並びが変わらない。

        対象のあいだに主従があるように見せないためでもある。
        """
        for r in ("zeta/last", "alpha/first", "Mid/dle"):
            analyze.analyze_and_cache([{"repo": r, "lines": ["main"]}], tmp_path, verbose=False)
        assert [e["repo"] for e in index_of(tmp_path)["analyses"]] == [
            "alpha/first",
            "Mid/dle",
            "zeta/last",
        ]

    def test_slug_avoids_path_separators(self, tmp_path, stub_run):
        analyze.analyze_and_cache(
            [{"repo": "Owner/Name.With.Dots", "lines": ["main"]}], tmp_path, verbose=False
        )
        entry = index_of(tmp_path)["analyses"][0]
        assert "/" not in entry["file"]
        assert (tmp_path / entry["file"]).exists()


class TestRefresh:
    def test_refresh_reanalyses_everything_in_the_index(self, tmp_path, stub_run):
        analyze.analyze_and_cache([{"repo": "owner/one", "lines": ["main"]}], tmp_path, verbose=False)
        analyze.analyze_and_cache(
            [{"repo": "owner/two", "lines": ["dev", "release"]}], tmp_path, verbose=False
        )
        stub_run.clear()

        targets = analyze.targets_from_index(tmp_path)
        analyze.analyze_and_cache(targets, tmp_path, verbose=False)

        assert sorted(r for r, _ in stub_run) == ["owner/one", "owner/two"]

    def test_refresh_preserves_line_configuration(self, tmp_path, stub_run):
        """再解析のときに、最初に指定した統合ブランチが引き継がれる。"""
        analyze.analyze_and_cache(
            [{"repo": "owner/two", "lines": ["dev", "release"], "include_forks": False}],
            tmp_path,
            verbose=False,
        )
        targets = analyze.targets_from_index(tmp_path)
        assert targets == [
            {"repo": "owner/two", "lines": ["dev", "release"], "include_forks": False}
        ]

    def test_refresh_on_empty_index_is_harmless(self, tmp_path):
        assert analyze.targets_from_index(tmp_path) == []


class TestResilience:
    @pytest.mark.parametrize("fail_at", ["prepare", "analyze"])
    def test_one_failure_does_not_stop_the_rest(self, tmp_path, monkeypatch, fail_at):
        """1 件が失敗しても他の解析は続き、索引にも残る。

        準備は先読みで並列に走るので、**準備段の失敗が他のリポジトリの
        パイプラインを壊さない**ことも確かめる（失敗した準備の枠が返らないと
        後続が永久に待つ）。
        """
        stub_pipeline(monkeypatch, fail={"owner/broken"}, fail_at=fail_at)
        rc = analyze.analyze_and_cache(
            [
                {"repo": "owner/broken", "lines": ["main"]},
                {"repo": "owner/fine", "lines": ["main"]},
                {"repo": "owner/also-fine", "lines": ["main"]},
            ],
            tmp_path,
            verbose=False,
        )
        assert rc == 1, "失敗があったことは終了コードで伝える"
        assert [e["repo"] for e in index_of(tmp_path)["analyses"]] == [
            "owner/also-fine",
            "owner/fine",
        ]

    def test_failure_keeps_previous_result_for_that_repo(self, tmp_path, monkeypatch):
        """再解析に失敗しても、前回の結果を消さない。"""
        stub_pipeline(monkeypatch)
        analyze.analyze_and_cache([{"repo": "owner/one", "lines": ["main"]}], tmp_path, verbose=False)

        stub_pipeline(monkeypatch, fail={"owner/one"})
        analyze.analyze_and_cache([{"repo": "owner/one", "lines": ["main"]}], tmp_path, verbose=False)

        assert [e["repo"] for e in index_of(tmp_path)["analyses"]] == ["owner/one"]
        assert (tmp_path / "owner-one.json").exists()

    def test_corrupt_index_is_rebuilt(self, tmp_path, stub_run):
        (tmp_path / "index.json").write_text("{ 壊れた JSON")
        analyze.analyze_and_cache([{"repo": "owner/one", "lines": ["main"]}], tmp_path, verbose=False)
        assert [e["repo"] for e in index_of(tmp_path)["analyses"]] == ["owner/one"]


class TestForget:
    """解析結果を git に置かない運用では、対象を外す手段が別に要る。"""

    def test_removes_entry_and_file(self, tmp_path, stub_run):
        analyze.analyze_and_cache([{"repo": "owner/one", "lines": ["main"]}], tmp_path, verbose=False)
        analyze.analyze_and_cache([{"repo": "owner/two", "lines": ["main"]}], tmp_path, verbose=False)

        assert analyze.forget("owner/one", tmp_path, verbose=False) == 0
        assert [e["repo"] for e in index_of(tmp_path)["analyses"]] == ["owner/two"]
        assert not (tmp_path / "owner-one.json").exists()
        assert (tmp_path / "owner-two.json").exists()

    def test_unknown_repo_reports_failure(self, tmp_path, stub_run):
        analyze.analyze_and_cache([{"repo": "owner/one", "lines": ["main"]}], tmp_path, verbose=False)
        assert analyze.forget("owner/absent", tmp_path, verbose=False) == 1
        assert [e["repo"] for e in index_of(tmp_path)["analyses"]] == ["owner/one"]

    def test_forgotten_repo_is_not_refreshed(self, tmp_path, stub_run):
        analyze.analyze_and_cache([{"repo": "owner/one", "lines": ["main"]}], tmp_path, verbose=False)
        analyze.analyze_and_cache([{"repo": "owner/two", "lines": ["main"]}], tmp_path, verbose=False)
        analyze.forget("owner/one", tmp_path, verbose=False)
        assert [t["repo"] for t in analyze.targets_from_index(tmp_path)] == ["owner/two"]


class TestUndetermined:
    """ベース衝突の PR を「独立」と混ぜないこと。

    着地tree が作れないと全ペアが degraded になり、衝突辺が 1 本も
    立たない。素朴にクラスタ分解すると「誰とも干渉しない＝独立」に
    見えてしまうが、実際は判定できていないだけ。
    """

    @staticmethod
    def _plan(pairs=()):
        from analyzer import dag, order
        from analyzer.model import Candidate, PullRequest

        def mk(n):
            return PullRequest(
                repo="o/r", number=n, title=f"#{n}", url="", author="a",
                head_repo="o/r", head_branch=f"b{n}", head_oid=f"{n:040d}",
                base_repo="o/r", base_branch="main",
            )

        graph = dag.build([mk(1), mk(2)], {dag.node_key("o/r", "main"): "main"})
        cands = [
            Candidate(id="o/r#1", head="a", line="main", landing_tree="t1"),
            Candidate(id="o/r#2", head="b", line="main", landing_tree=None),
        ]
        return order.plan_line("main", cands, list(pairs), graph)

    def test_base_conflict_pr_is_not_called_independent(self):
        from analyzer.model import PairResult, Relation

        plan = self._plan([PairResult(a="o/r#1", b="o/r#2", relation=Relation.DEGRADED)])
        assert plan.independent == ["o/r#1"]
        assert plan.undetermined == ["o/r#2"]

    def test_every_pr_appears_exactly_once_in_the_order(self):
        plan = self._plan()
        for preset in plan.presets.values():
            assert sorted(preset["order"]) == ["o/r#1", "o/r#2"]


class TestClusterScope:
    """クラスタは「意図しない干渉」だけで作る。

    スタックは作者が意図して積んだ依存で、順序はすでに決まっている。
    連結成分に混ぜると、衝突が 1 件も無い鎖が「順序を議論すべき
    クラスタ」として現れてしまう。
    """

    def test_pure_stack_chain_is_not_a_cluster(self):
        from analyzer import order
        from analyzer.model import PairResult, Relation

        pairs = [PairResult(a="r#1", b="r#2", relation=Relation.STACKED)]
        clusters, rest = order.build_clusters(["r#1", "r#2"], pairs)
        assert clusters == []
        assert sorted(rest) == ["r#1", "r#2"]

    def test_conflict_still_forms_a_cluster(self):
        from analyzer import order
        from analyzer.model import Level, PairResult, Relation

        pairs = [PairResult(a="r#1", b="r#2", relation=Relation.COMPUTED, level=Level.L2)]
        clusters, rest = order.build_clusters(["r#1", "r#2"], pairs)
        assert len(clusters) == 1
        assert sorted(clusters[0].members) == ["r#1", "r#2"]
        assert rest == []

    def test_stack_order_is_still_enforced(self):
        """クラスタから外しても、親が先という制約は残る。"""
        from analyzer import dag, order
        from analyzer.model import Candidate, PullRequest

        def mk(n, base):
            return PullRequest(
                repo="o/r", number=n, title=f"#{n}", url="", author="a",
                head_repo="o/r", head_branch=f"b{n}", head_oid=f"{n:040d}",
                base_repo="o/r", base_branch=base,
            )

        graph = dag.build([mk(1, "main"), mk(2, "b1")], {dag.node_key("o/r", "main"): "main"})
        cands = [
            Candidate(id="o/r#1", head="a", line="main", landing_tree="t1"),
            Candidate(id="o/r#2", head="b", line="main", landing_tree="t2",
                      ancestors=frozenset({"o/r#1"})),
        ]
        plan = order.plan_line("main", cands, [], graph)
        assert plan.clusters == []
        for preset in plan.presets.values():
            o = preset["order"]
            assert o.index("o/r#1") < o.index("o/r#2"), "親が先という制約は保たれる"


class TestStackOrderInvariant:
    """親 PR は子より必ず先。クラスタ構成を変えても壊れてはいけない。"""

    def test_parent_precedes_child_across_groups(self):
        from analyzer import dag, order
        from analyzer.model import Candidate, Level, PairResult, PullRequest, Relation

        def mk(n, base):
            return PullRequest(
                repo="o/r", number=n, title=f"#{n}", url="", author="a",
                head_repo="o/r", head_branch=f"b{n}", head_oid=f"{n:040d}",
                base_repo="o/r", base_branch=base,
            )

        # #1 <- #2 のスタック（干渉なし）。#3 と #4 は互いに衝突しクラスタを作る。
        graph = dag.build(
            [mk(1, "main"), mk(2, "b1"), mk(3, "main"), mk(4, "main")],
            {dag.node_key("o/r", "main"): "main"},
        )
        cands = [
            Candidate(id="o/r#1", head="a", line="main", landing_tree="t1"),
            Candidate(id="o/r#2", head="b", line="main", landing_tree="t2",
                      ancestors=frozenset({"o/r#1"})),
            Candidate(id="o/r#3", head="c", line="main", landing_tree="t3"),
            Candidate(id="o/r#4", head="d", line="main", landing_tree="t4"),
        ]
        pairs = [PairResult(a="o/r#3", b="o/r#4", relation=Relation.COMPUTED, level=Level.L2)]
        plan = order.plan_line("main", cands, pairs, graph)

        assert [c.members for c in plan.clusters] == [["o/r#3", "o/r#4"]]
        for name, preset in plan.presets.items():
            pos = {pid: i for i, pid in enumerate(preset["order"])}
            assert pos["o/r#1"] < pos["o/r#2"], f"{name}: 親が子より後ろ"

    def test_enforce_predecessors_keeps_order_when_possible(self):
        from analyzer.order import enforce_predecessors

        seq = ["a", "b", "c", "d"]
        assert enforce_predecessors(seq, {}) == seq
        # c は a を待つ -> a を前に出すが、他は動かさない
        assert enforce_predecessors(["c", "a", "b"], {"c": {"a"}}) == ["a", "c", "b"]

    def test_cycle_does_not_hang(self):
        from analyzer.order import enforce_predecessors

        out = enforce_predecessors(["a", "b"], {"a": {"b"}, "b": {"a"}})
        assert sorted(out) == ["a", "b"]


class TestLineReplacementNotice:
    """統合ラインの指定を置き換えるときは、必ず気づけるようにする。

    別の実行が違う lines で走ると、蓄積されている指定は黙って上書き
    される。実際にこれで設定が失われた。
    """

    def test_replacement_is_reported(self, tmp_path, stub_run, capsys):
        analyze.analyze_and_cache(
            [{"repo": "o/r", "lines": ["release"]}], tmp_path, verbose=True
        )
        analyze.analyze_and_cache(
            [{"repo": "o/r", "lines": ["main"]}], tmp_path, verbose=True
        )
        err = capsys.readouterr().err
        assert "置き換えます" in err
        assert "release" in err and "main" in err

    def test_same_lines_is_quiet(self, tmp_path, stub_run, capsys):
        for _ in range(2):
            analyze.analyze_and_cache(
                [{"repo": "o/r", "lines": ["main"]}], tmp_path, verbose=True
            )
        assert "置き換えます" not in capsys.readouterr().err


class TestPrefetchPipeline:
    """準備（ネットワーク律速）を先読みして解析（CPU 律速）と重ねる部分。

    ここが壊れると、症状は「遅い」ではなく「順序が揺れる」「ディスクが
    埋まる」「1 件の失敗で全体が止まる」になるので、機構ごとに押さえる。
    """

    def test_results_follow_target_order_not_completion_order(self, tmp_path, monkeypatch):
        """先に準備が終わった方から処理してはいけない。

        索引の並びは書き出し時にソートされるが、解析の順序が揺れると
        ログの読み方も、失敗時にどこまで進んだかの解釈も変わる。
        """
        import time

        order: list[str] = []

        def _prepare(repo, lines, **kw):
            # 先頭の準備をいちばん遅くする（完了順 = 逆順になるように）
            time.sleep(0.05 if repo == "owner/a" else 0.0)
            return FakePrepared(repo, lines)

        def _analyze(prepared, **kw):
            order.append(prepared.target)
            return fake_analysis(prepared.target)

        monkeypatch.setattr(analyze, "prepare", _prepare)
        monkeypatch.setattr(analyze, "analyze_prepared", _analyze)
        analyze.analyze_and_cache(
            [{"repo": f"owner/{n}", "lines": ["main"]} for n in ("a", "b", "c", "d")],
            tmp_path,
            verbose=False,
        )
        assert order == ["owner/a", "owner/b", "owner/c", "owner/d"]

    def test_prefetch_is_bounded(self, tmp_path, monkeypatch):
        """未消費の準備が積み上がらない。

        作業リポジトリは大きい（実測で 1 件 777MB）ので、先読みが
        無制限だと対象が増えたときにディスクを食い潰す。
        """
        import threading

        live = 0
        peak = 0
        lock = threading.Lock()

        def _prepare(repo, lines, **kw):
            nonlocal live, peak
            with lock:
                live += 1
                peak = max(peak, live)
            return FakePrepared(repo, lines)

        def _analyze(prepared, **kw):
            nonlocal live
            with lock:
                live -= 1
            return fake_analysis(prepared.target)

        monkeypatch.setattr(analyze, "prepare", _prepare)
        monkeypatch.setattr(analyze, "analyze_prepared", _analyze)
        analyze.analyze_and_cache(
            [{"repo": f"owner/r{i}", "lines": ["main"]} for i in range(12)],
            tmp_path,
            verbose=False,
        )
        assert peak <= analyze._PREFETCH_DEPTH


class TestWriteFailureIsIsolated:
    """書き出しの失敗も「1 件の失敗」に留める。

    先読みで作業リポジトリを複数抱えるようになったので、いちばん起こりやすい
    失敗はディスク不足である。それで全体が止まると、無関係なリポジトリの
    結果まで古いまま取り残される。
    """

    def test_unwritable_output_does_not_stop_the_rest(self, tmp_path, monkeypatch):
        stub_pipeline(monkeypatch)
        real_write = analyze.Path.write_text

        def _write(self, *a, **kw):
            if self.name == "owner-broken.json":
                raise OSError("No space left on device")
            return real_write(self, *a, **kw)

        monkeypatch.setattr(analyze.Path, "write_text", _write)
        rc = analyze.analyze_and_cache(
            [
                {"repo": "owner/broken", "lines": ["main"]},
                {"repo": "owner/fine", "lines": ["main"]},
            ],
            tmp_path,
            verbose=False,
        )
        assert rc == 1
        assert [e["repo"] for e in index_of(tmp_path)["analyses"]] == ["owner/fine"]

    def test_prefetched_work_is_discarded_when_the_loop_aborts(self, tmp_path, monkeypatch):
        """途中で想定外に抜けても、先読み済みの作業ディレクトリを残さない。

        1 件が数百 MB あるので、残すと次の実行のディスクを削る。しかも最も
        起こりやすい引き金がディスク不足なので、放っておくと悪循環になる。

        `KeyboardInterrupt` で落とす —— `except Exception` では捕まらない経路を
        通したいため（ジェネレータの後片付けが GC 待ちになっていないことの確認）。
        """
        import threading

        discarded: list[str] = []
        prefetched = threading.Event()

        class Tracked(FakePrepared):
            def discard(self) -> None:
                discarded.append(self.target)

        def _prepare(repo, lines, **kw):
            if repo == "owner/r1":
                prefetched.set()
            return Tracked(repo, lines)

        def _boom(prepared, **kw):
            # 先読み分が実際に出来上がってから落ちる（出来ていなければ
            # 「片付けるものが無い」だけで、この検査が空回りする）
            assert prefetched.wait(10), "先読みが走っていない"
            raise KeyboardInterrupt("Ctrl-C")

        monkeypatch.setattr(analyze, "prepare", _prepare)
        monkeypatch.setattr(analyze, "analyze_prepared", _boom)
        with pytest.raises(KeyboardInterrupt):
            analyze.analyze_and_cache(
                [{"repo": f"owner/r{i}", "lines": ["main"]} for i in range(4)],
                tmp_path,
                verbose=False,
            )
        assert discarded == ["owner/r1"], "先読み済みの作業リポジトリが片付けられていない"

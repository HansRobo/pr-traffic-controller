"""索引（解析結果のキャッシュ）の蓄積動作。

解析対象をリポジトリ内に固定で持たず、実行のたびに索引へ積み上げていく
方式なので、「前に解析したものが消えない」ことが要になる。
実際の解析は走らせず、`prepare` / `analyze_prepared` を差し替えて検証する。
"""

from __future__ import annotations

import json
import threading
import time

import pytest

from analyzer import analyze
from analyzer.report import SCHEMA_VERSION


def fake_analysis(repo: str, generated_at: str = "2026-01-01T00:00:00Z") -> dict:
    return {
        # 本番の定数を引く。ここをリテラルにすると、bump したとき
        # テストだけ旧値のまま黙って通り続ける
        "schema_version": SCHEMA_VERSION,
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

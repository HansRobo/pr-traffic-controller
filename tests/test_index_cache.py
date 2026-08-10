"""索引（解析結果のキャッシュ）の蓄積動作。

解析対象をリポジトリ内に固定で持たず、実行のたびに索引へ積み上げていく
方式なので、「前に解析したものが消えない」ことが要になる。
実際の解析は走らせず、`run` を差し替えて検証する。
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


@pytest.fixture
def stub_run(monkeypatch):
    """`analyze.run` を差し替えて、呼ばれたリポジトリを記録する。"""
    calls: list[tuple[str, list[str]]] = []

    def _run(repo, lines, **kw):
        calls.append((repo, list(lines)))
        return fake_analysis(repo)

    monkeypatch.setattr(analyze, "run", _run)
    return calls


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
    def test_one_failure_does_not_stop_the_rest(self, tmp_path, monkeypatch):
        """1 件が失敗しても他の解析は続き、索引にも残る。"""

        def _run(repo, lines, **kw):
            if repo == "owner/broken":
                raise RuntimeError("clone に失敗")
            return fake_analysis(repo)

        monkeypatch.setattr(analyze, "run", _run)
        rc = analyze.analyze_and_cache(
            [
                {"repo": "owner/broken", "lines": ["main"]},
                {"repo": "owner/fine", "lines": ["main"]},
            ],
            tmp_path,
            verbose=False,
        )
        assert rc == 1, "失敗があったことは終了コードで伝える"
        assert [e["repo"] for e in index_of(tmp_path)["analyses"]] == ["owner/fine"]

    def test_failure_keeps_previous_result_for_that_repo(self, tmp_path, monkeypatch):
        """再解析に失敗しても、前回の結果を消さない。"""
        monkeypatch.setattr(analyze, "run", lambda repo, lines, **kw: fake_analysis(repo))
        analyze.analyze_and_cache([{"repo": "owner/one", "lines": ["main"]}], tmp_path, verbose=False)

        def _boom(repo, lines, **kw):
            raise RuntimeError("一時的な失敗")

        monkeypatch.setattr(analyze, "run", _boom)
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

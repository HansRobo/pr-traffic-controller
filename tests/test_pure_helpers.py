"""git を起こさずに検証できる純関数と、その周辺の小さな契約。

いずれも「明示された規約はあるが、破っても静かに間違うだけ」のものを集めてある。
`split_node` の分割規則、ペアキーの向き、干渉を計算しない条件、`merge_tree` の
メモ化 —— どれも壊れても例外は出ない。
"""

from __future__ import annotations

import pytest

from analyzer import dag, interference
from analyzer.model import Candidate, Level, PullRequest, Relation, pair_key


class TestSplitNode:
    """`node_key` の逆変換。`analyze` の 3 箇所が依存している。"""

    @pytest.mark.parametrize(
        "repo,branch",
        [
            ("o/r", "main"),
            ("o/r", "feature/x"),
            # ブランチ名に `:` が入りうるのが要点。分割は最初の 1 つだけ
            ("o/r", "feat:x"),
            ("o/r", "a:b:c"),
            ("owner-with-dash/repo.dots", "release/2.0"),
        ],
    )
    def test_round_trips_with_node_key(self, repo, branch):
        assert dag.split_node(dag.node_key(repo, branch)) == (repo, branch)

    def test_splits_on_the_first_colon_only(self):
        assert dag.split_node("o/r:feat:x") == ("o/r", "feat:x")

    def test_missing_colon_yields_empty_branch(self):
        """`partition` なので `:` が無くても例外にしない。"""
        assert dag.split_node("o/r") == ("o/r", "")


class TestPairKey:
    """ペアを id 2 つから引くときの向きの規約。

    引き側と作り側で規約がずれると `pair_map.get()` が全部 miss し、
    衝突ペアがコスト 0 として扱われる（例外は出ない）。
    """

    def test_is_symmetric(self):
        assert pair_key("b", "a") == pair_key("a", "b")

    def test_is_sorted(self):
        assert pair_key("b", "a") == ("a", "b")

    def test_same_id(self):
        assert pair_key("a", "a") == ("a", "a")

    def test_matches_pair_result_key(self):
        from analyzer.model import PairResult

        p = PairResult(a="o/r#9", b="o/r#2", relation=Relation.COMPUTED)
        assert p.key() == pair_key("o/r#9", "o/r#2")


def cand(n: int, *, line="main", tree="t", ancestors=frozenset()) -> Candidate:
    return Candidate(
        id=f"o/r#{n}", head=f"h{n}", line=line, landing_tree=tree, ancestors=ancestors
    )


class TestBlockingRelation:
    """「このペアは merge-tree まで進むか」の唯一の情報源。

    `analyze_pair` と `_prewarm_hunks` の両方がこれを引く。ずれると
    温めが効かなくなるだけなので、気づけない。
    """

    def test_computable_pair_returns_none(self):
        assert interference.blocking_relation(cand(1), cand(2)) is None

    def test_ancestor_is_stacked(self):
        a, b = cand(1), cand(2, ancestors=frozenset({"o/r#1"}))
        assert interference.blocking_relation(a, b) is Relation.STACKED

    def test_stacked_is_symmetric(self):
        a, b = cand(1), cand(2, ancestors=frozenset({"o/r#1"}))
        assert interference.blocking_relation(b, a) is Relation.STACKED

    def test_different_lines_are_incomparable(self):
        assert (
            interference.blocking_relation(cand(1), cand(2, line="dev"))
            is Relation.INCOMPARABLE
        )

    def test_base_conflict_is_degraded(self):
        assert (
            interference.blocking_relation(cand(1), cand(2, tree=None))
            is Relation.DEGRADED
        )

    def test_stacked_wins_over_base_conflict(self):
        """祖先関係が先に判定される（着地tree が無くても STACKED）。"""
        a = cand(1)
        b = cand(2, tree=None, ancestors=frozenset({"o/r#1"}))
        assert interference.blocking_relation(a, b) is Relation.STACKED


def mk_pr(n: int, *, base_branch: str, head_branch: str | None = None) -> PullRequest:
    return PullRequest(
        repo="o/r",
        number=n,
        title=f"#{n}",
        url="",
        author="a",
        head_repo="o/r",
        head_branch=head_branch or f"b{n}",
        head_oid=f"{n:040d}",
        base_repo="o/r",
        base_branch=base_branch,
    )


class TestBaseIndex:
    """`children_of` は索引を引く。全 PR 走査だと `descendants_of` が O(n²)。"""

    def _graph(self):
        # #1 <- #2, #3 （#2 と #3 が #1 の子）、#4 は独立
        return dag.build(
            [
                mk_pr(1, base_branch="main"),
                mk_pr(2, base_branch="b1"),
                mk_pr(3, base_branch="b1"),
                mk_pr(4, base_branch="main"),
            ],
            {dag.node_key("o/r", "main"): "main"},
        )

    def test_children_are_found(self):
        assert self._graph().children_of("o/r#1") == ["o/r#2", "o/r#3"]

    def test_leaf_has_no_children(self):
        assert self._graph().children_of("o/r#2") == []

    def test_base_index_is_populated(self):
        g = self._graph()
        assert sorted(g.base_index[dag.node_key("o/r", "b1")]) == ["o/r#2", "o/r#3"]

    def test_descendants_are_transitive(self):
        g = dag.build(
            [
                mk_pr(1, base_branch="main"),
                mk_pr(2, base_branch="b1"),
                mk_pr(3, base_branch="b2"),
            ],
            {dag.node_key("o/r", "main"): "main"},
        )
        assert g.descendants_of("o/r#1") == {"o/r#2", "o/r#3"}

    def test_self_is_not_its_own_child(self):
        """head と base が同じ PR でも自分を子にしない。"""
        g = dag.build(
            [mk_pr(1, base_branch="b1", head_branch="b1")],
            {dag.node_key("o/r", "main"): "main"},
        )
        assert g.children_of("o/r#1") == []


class TestMergeTreeMemoization:
    """`Repo.merge_tree` のメモ化。

    効かなくなっても性能が落ちるだけで誰も気づかないので、ここで固定する。
    docstring が「呼び出し側は OID を渡すこと」という前提を宣言しているが、
    それを強制する仕組みは無い —— **キーが不変であることが前提**という事実を
    テストとしても残しておく。
    """

    def _repo(self, monkeypatch):
        from analyzer.gitops import Repo

        repo = Repo(".")
        calls: list[tuple] = []

        def fake(self, *, merge_base, ours, theirs):
            calls.append((merge_base, ours, theirs))
            return f"result:{merge_base}/{ours}/{theirs}"

        monkeypatch.setattr(Repo, "_merge_tree_uncached", fake)
        return repo, calls

    def test_same_key_runs_git_once(self, monkeypatch):
        repo, calls = self._repo(monkeypatch)
        a = repo.merge_tree(merge_base="mb", ours="o", theirs="t")
        b = repo.merge_tree(merge_base="mb", ours="o", theirs="t")
        assert a == b
        assert len(calls) == 1

    def test_different_keys_are_not_conflated(self, monkeypatch):
        repo, calls = self._repo(monkeypatch)
        repo.merge_tree(merge_base="mb", ours="o", theirs="t")
        repo.merge_tree(merge_base="mb", ours="t", theirs="o")
        repo.merge_tree(merge_base="other", ours="o", theirs="t")
        assert len(calls) == 3

    def test_order_of_ours_and_theirs_matters(self, monkeypatch):
        """マージは可換ではないので、キーを正規化してはいけない。"""
        repo, _ = self._repo(monkeypatch)
        ab = repo.merge_tree(merge_base="mb", ours="a", theirs="b")
        ba = repo.merge_tree(merge_base="mb", ours="b", theirs="a")
        assert ab != ba


class TestClassifyFileUsesModel:
    """ステージ集合の読み方は `ConflictFile.is_structural` が持つ。"""

    def _cf(self, stages, types=("CONFLICT (contents)",)):
        from analyzer.model import ConflictFile

        return ConflictFile(path="x.py", stages=frozenset(stages), types=types)

    def test_full_stages_are_content_conflict(self):
        assert interference.classify_file(self._cf({1, 2, 3})) is Level.L2

    @pytest.mark.parametrize("stages", [{2, 3}, {1, 3}, {1, 2}, {1}, {3}])
    def test_missing_stage_is_structural(self, stages):
        assert interference.classify_file(self._cf(stages)) is Level.L3

    def test_no_stages_falls_back_to_type(self):
        assert interference.classify_file(self._cf(set())) is Level.L2
        assert (
            interference.classify_file(
                self._cf(set(), types=("CONFLICT (rename/rename)",))
            )
            is Level.L3
        )

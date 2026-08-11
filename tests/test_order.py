"""マージ順の組み立て（クラスタ分解・プリセット順序・先行制約）。

以前は `test_index_cache.py` に同居していたが、索引キャッシュとは何の関係も無く
（`analyze` も `tmp_path` も使わない）、`order.py` を触る人がそのファイルを
開かないので移した。
"""

from __future__ import annotations

from analyzer import dag, order
from analyzer.model import Level, PairResult, Relation

from .factories import make_candidate, make_pr


def graph_of(*bases: str):
    """`bases[i]` を base ブランチに持つ PR #1..#n のグラフ。

    `graph_of("main", "b1")` なら #1 が main、#2 が #1 の head に載る。
    """
    prs = [make_pr(n, base_branch=b) for n, b in enumerate(bases, start=1)]
    return dag.build(prs, {dag.node_key("o/r", "main"): "main"})


class TestUndetermined:
    """ベース衝突の PR を「独立」と混ぜないこと。

    着地tree が作れないと全ペアが degraded になり、衝突辺が 1 本も
    立たない。素朴にクラスタ分解すると「誰とも干渉しない＝独立」に
    見えてしまうが、実際は判定できていないだけ。
    """

    @staticmethod
    def _plan(pairs=()):
        cands = [
            make_candidate(1),
            make_candidate(2, landing_tree=None),  # ベース衝突
        ]
        return order.plan_line(cands, list(pairs), graph_of("main", "main"))

    def test_base_conflict_pr_is_not_called_independent(self):
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
        pairs = [PairResult(a="r#1", b="r#2", relation=Relation.STACKED)]
        clusters, rest = order.build_clusters(["r#1", "r#2"], pairs)
        assert clusters == []
        assert sorted(rest) == ["r#1", "r#2"]

    def test_conflict_still_forms_a_cluster(self):
        pairs = [PairResult(a="r#1", b="r#2", relation=Relation.COMPUTED, level=Level.L2)]
        clusters, rest = order.build_clusters(["r#1", "r#2"], pairs)
        assert len(clusters) == 1
        assert sorted(clusters[0].members) == ["r#1", "r#2"]
        assert rest == []

    def test_stack_order_is_still_enforced(self):
        """クラスタから外しても、親が先という制約は残る。"""
        cands = [
            make_candidate(1),
            make_candidate(2, ancestors=frozenset({"o/r#1"})),
        ]
        plan = order.plan_line(cands, [], graph_of("main", "b1"))
        assert plan.clusters == []
        for preset in plan.presets.values():
            o = preset["order"]
            assert o.index("o/r#1") < o.index("o/r#2"), "親が先という制約は保たれる"


class TestStackOrderInvariant:
    """親 PR は子より必ず先。クラスタ構成を変えても壊れてはいけない。"""

    def test_parent_precedes_child_across_groups(self):
        # #1 <- #2 のスタック（干渉なし）。#3 と #4 は互いに衝突しクラスタを作る。
        cands = [
            make_candidate(1),
            make_candidate(2, ancestors=frozenset({"o/r#1"})),
            make_candidate(3),
            make_candidate(4),
        ]
        pairs = [PairResult(a="o/r#3", b="o/r#4", relation=Relation.COMPUTED, level=Level.L2)]
        plan = order.plan_line(cands, pairs, graph_of("main", "b1", "main", "main"))

        assert [c.members for c in plan.clusters] == [["o/r#3", "o/r#4"]]
        for name, preset in plan.presets.items():
            pos = {pid: i for i, pid in enumerate(preset["order"])}
            assert pos["o/r#1"] < pos["o/r#2"], f"{name}: 親が子より後ろ"

    def test_enforce_predecessors_keeps_order_when_possible(self):
        seq = ["a", "b", "c", "d"]
        assert order.enforce_predecessors(seq, {}) == seq
        # c は a を待つ -> a を前に出すが、他は動かさない
        assert order.enforce_predecessors(["c", "a", "b"], {"c": {"a"}}) == ["a", "c", "b"]

    def test_cycle_does_not_hang(self):
        out = order.enforce_predecessors(["a", "b"], {"a": {"b"}, "b": {"a"}})
        assert sorted(out) == ["a", "b"]

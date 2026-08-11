"""層3: スタック DAG の構造テスト。

git を必要としない（PR オブジェクトを直接与える）ので、どの環境でも走る。
テストケースは、フォークを跨いでスタックした PR 群を扱う実運用で
遭遇する構造を写したもの。
"""

from __future__ import annotations

import pytest

from analyzer import dag
from analyzer.model import PullRequest

from .factories import make_pr

UP = "upstream/project"   # 上流リポジトリ
FK = "contributor/project"  # 貢献者のフォーク

LINES = {
    dag.node_key(UP, "main"): "main",
    dag.node_key(UP, "develop"): "develop",
}


def pr(repo, number, head_repo, head_branch, base_repo, base_branch, **kw) -> PullRequest:
    """このファイル向けの位置引数版。フィールドの穴埋めは factories が持つ。"""
    return make_pr(
        number,
        repo=repo,
        head_repo=head_repo,
        head_branch=head_branch,
        base_repo=base_repo,
        base_branch=base_branch,
        **kw,
    )


# --- 代表的な鎖 -------------------------------------------------------

def chain_a() -> list[PullRequest]:
    """main <- #10 <- #11 <- fork#2（リポジトリ跨ぎ3段）。"""
    return [
        pr(UP, 10, UP, "feat/a", UP, "main"),
        pr(UP, 11, FK, "feat/b", UP, "feat/a"),
        pr(FK, 2, FK, "feat/c", FK, "feat/b"),
    ]


def chain_b() -> list[PullRequest]:
    """main <- #20 <- fork#3 <- #4 <- #5（4段）。"""
    return [
        pr(UP, 20, FK, "feat/d", UP, "main"),
        pr(FK, 3, FK, "feat/e", FK, "feat/d"),
        pr(FK, 4, FK, "feat/f", FK, "feat/e"),
        pr(FK, 5, FK, "feat/g", FK, "feat/f"),
    ]


def chain_c() -> list[PullRequest]:
    """同一リポジトリ内の3段: #30 <- #31 <- #32。"""
    return [
        pr(UP, 30, UP, "feat/h", UP, "main"),
        pr(UP, 31, UP, "feat/i", UP, "feat/h"),
        pr(UP, 32, UP, "feat/j", UP, "feat/i"),
    ]


class TestCrossRepositoryChains:
    def test_chain_a_resolves_through_fork(self):
        g = dag.build(chain_a(), LINES)
        r = g.resolutions[f"{FK}#2"]
        assert r.line == "main", "フォーク側の PR も統合ラインを継承する"
        assert r.ancestors == (f"{UP}#10", f"{UP}#11")
        assert r.depth == 2
        assert r.resolution == "resolved"

    def test_chain_b_depth_four(self):
        g = dag.build(chain_b(), LINES)
        r = g.resolutions[f"{FK}#5"]
        assert r.line == "main"
        assert r.ancestors == (f"{UP}#20", f"{FK}#3", f"{FK}#4")
        assert r.depth == 3

    def test_chain_c_same_repository(self):
        g = dag.build(chain_c(), LINES)
        assert g.resolutions[f"{UP}#32"].depth == 2
        assert g.resolutions[f"{UP}#32"].line == "main"

    def test_all_chains_together(self):
        g = dag.build(chain_a() + chain_b() + chain_c(), LINES)
        assert all(r.line == "main" for r in g.resolutions.values())

    def test_line_is_inherited_not_read_from_base(self):
        """スタックした PR の base 欄は統合ラインではない。

        base 欄を直接見る実装だと、これらの PR はどのラインにも属さず
        順序推奨から丸ごと消える。
        """
        g = dag.build(chain_b(), LINES)
        p = g.prs[f"{FK}#5"]
        assert p.base_branch == "feat/f"
        assert g.line_of(f"{FK}#5") == "main"


class TestDescendants:
    def test_blocks_count(self):
        """#20 が止まると 3 件がマージ不可能になる（blocks 指標の根拠）。"""
        g = dag.build(chain_b(), LINES)
        assert g.descendants_of(f"{UP}#20") == {f"{FK}#3", f"{FK}#4", f"{FK}#5"}
        assert g.descendants_of(f"{FK}#5") == set()

    def test_direct_children(self):
        g = dag.build(chain_a(), LINES)
        assert g.children_of(f"{UP}#10") == [f"{UP}#11"]


class TestDuplicateHead:
    """同一コミットが 2 つの PR の head になっている代表的なケース。

    フォークの feat/c ブランチが、上流側 #12 (base=feat/a) と
    フォーク側 #2 (base=feat/b) の両方の head になっている。
    """

    @pytest.fixture
    def g(self):
        return dag.build(chain_a() + [pr(UP, 12, FK, "feat/c", UP, "feat/a")], LINES)

    def test_warning_is_emitted(self, g):
        w = [x for x in g.warnings if x.kind == "duplicate_pr_head"]
        assert len(w) == 1
        assert set(w[0].subjects) == {f"{UP}#12", f"{FK}#2"}

    def test_neither_pr_is_dropped(self, g):
        assert f"{UP}#12" in g.resolutions
        assert f"{FK}#2" in g.resolutions

    def test_both_resolve_to_same_line(self, g):
        assert g.line_of(f"{UP}#12") == "main"
        assert g.line_of(f"{FK}#2") == "main"


class TestOrphanBase:
    """base ブランチは存在するが対応するオープン PR が無いケース。

    #40 の base である feat/orphan-parent のような場合。
    """

    def orphan_set(self) -> list[PullRequest]:
        return [pr(UP, 40, UP, "feat/k", UP, "feat/orphan-parent")]

    @pytest.fixture
    def g(self):
        return dag.build(self.orphan_set(), LINES)

    def test_does_not_raise(self, g):
        assert f"{UP}#40" in g.resolutions

    def test_warns_and_marks_ambiguous_without_inference(self, g):
        assert [w for w in g.warnings if w.kind == "orphan_base_branch"]
        assert g.resolutions[f"{UP}#40"].resolution == "ambiguous"
        assert g.line_of(f"{UP}#40") is None

    def test_line_can_be_inferred_from_git(self):
        """git 側で所属ラインを推定できれば解析に載せられる。"""
        g = dag.build(self.orphan_set(), LINES, infer_line=lambda node: "main")
        assert g.resolutions[f"{UP}#40"].resolution == "inferred"
        assert g.line_of(f"{UP}#40") == "main"


class TestPathological:
    def test_cycle_does_not_hang_or_raise(self):
        cyclic = [
            pr(UP, 900, UP, "a", UP, "b"),
            pr(UP, 901, UP, "b", UP, "a"),
        ]
        g = dag.build(cyclic, LINES)
        assert [w for w in g.warnings if w.kind == "cycle_detected"]
        assert all(r.resolution == "cyclic" for r in g.resolutions.values())

    def test_two_lines_are_kept_separate(self):
        """別ラインの PR は所属も root も混ざらない。

        root が分かれていることまでここで見る（別ラインどうしは
        `INCOMPARABLE` になるべきで、その根拠が root の相違）。
        """
        prs = [
            pr(UP, 1, UP, "x", UP, "main"),
            pr(UP, 2, UP, "y", UP, "develop"),
        ]
        g = dag.build(prs, LINES)
        assert g.line_of(f"{UP}#1") == "main"
        assert g.line_of(f"{UP}#2") == "develop"
        assert g.resolutions[f"{UP}#1"].root_node != g.resolutions[f"{UP}#2"].root_node

    def test_empty_input(self):
        g = dag.build([], LINES)
        assert g.resolutions == {}
        assert g.warnings == []


class TestForkLineAliases:
    """フォークの同名ブランチを同じ統合ラインとして扱う。

    フォーク内で完結する PR（base がフォークの main）は、ノードキーに
    リポジトリが入るため `fork:main` という別ノードになる。別名を
    張らないと「指定されていない統合ライン」と判定され、まるごと
    解析から除外されてしまう。
    """

    def aliased_lines(self) -> dict:
        return {**LINES, dag.node_key(FK, "main"): "main"}

    def test_fork_internal_pr_resolves_to_upstream_line(self):
        prs = [pr(FK, 7, FK, "feat/x", FK, "main")]
        g = dag.build(prs, self.aliased_lines())
        assert g.line_of(f"{FK}#7") == "main"
        assert g.resolutions[f"{FK}#7"].resolution == "resolved"

    def test_without_alias_it_is_unresolved(self):
        """別名を張らなければ解決できない（回帰の対比）。"""
        prs = [pr(FK, 7, FK, "feat/x", FK, "main")]
        g = dag.build(prs, LINES)
        assert g.line_of(f"{FK}#7") is None

    def test_fork_stack_on_fork_main_keeps_depth(self):
        prs = [
            pr(FK, 7, FK, "feat/x", FK, "main"),
            pr(FK, 8, FK, "feat/y", FK, "feat/x"),
        ]
        g = dag.build(prs, self.aliased_lines())
        assert g.line_of(f"{FK}#8") == "main"
        assert g.resolutions[f"{FK}#8"].ancestors == (f"{FK}#7",)


class TestDuplicateHeadWording:
    """head が同じでも、マージ先が違えば重複とは限らない。

    同じ内容を「レビュー用にフォーク内へ」「着地用に上流へ」と二重に
    出すのは正当なやり方で、掃除の対象ではない。
    """

    def test_same_base_is_a_cleanup_candidate(self):
        prs = [
            pr(UP, 1, FK, "feat/x", UP, "main"),
            pr(UP, 2, FK, "feat/x", UP, "main"),
        ]
        g = dag.build(prs, LINES)
        w = next(x for x in g.warnings if x.kind == "duplicate_pr_head")
        assert w.severity == "warn"
        assert "掃除の候補" in w.detail

    def test_different_base_is_not_asserted_to_be_duplicate(self):
        prs = [
            pr(UP, 1, FK, "feat/x", UP, "main"),
            pr(FK, 2, FK, "feat/x", FK, "feat/parent"),
        ]
        g = dag.build(prs, LINES)
        w = next(x for x in g.warnings if x.kind == "duplicate_pr_head")
        assert w.severity == "info"
        assert "重複とは限らない" in w.detail

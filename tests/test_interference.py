"""層2: 合成リポジトリに対する統合テスト。

実際の git を動かして L0〜L3 とセマンティック警告が再現できることを
確認する。git >= 2.40 が必要なので、古い git の環境では自動的に skip
される（ローカルの git 2.34 など）。docker での実行方法は run-tests-docker.sh を参照。
"""

from __future__ import annotations

import pytest

from analyzer import interference
from analyzer.gitops import MIN_GIT_VERSION, Repo, git_version
from analyzer.model import Level, Relation, WarningKind

from .repofixture import build

pytestmark = pytest.mark.requires_git


def _git_too_old() -> bool:
    try:
        return git_version()[:2] < MIN_GIT_VERSION
    except Exception:
        return True


pytest.mark.skipif(_git_too_old(), reason="git >= 2.40 が必要")


@pytest.fixture(scope="module")
def repo(tmp_path_factory) -> Repo:
    if _git_too_old():
        pytest.skip("git >= 2.40 が必要")
    root = tmp_path_factory.mktemp("fixture-repo")
    build(root)
    return Repo(root)


@pytest.fixture(scope="module")
def line(repo: Repo) -> str:
    return repo.rev_parse("main")


def cand(repo: Repo, line: str, branch: str, **kw):
    return interference.build_candidate(repo, line, branch, repo.rev_parse(branch), **kw)


def pair(repo: Repo, line: str, a: str, b: str):
    return interference.analyze_pair(repo, line, cand(repo, line, a), cand(repo, line, b))


class TestLevels:
    def test_l0_no_file_overlap(self, repo, line):
        r = pair(repo, line, "a", "b")
        assert r.relation is Relation.COMPUTED
        assert r.level is Level.L0
        assert r.overlap_files == frozenset()

    def test_l1_same_file_distant_regions(self, repo, line):
        r = pair(repo, line, "b", "c")
        assert r.level is Level.L1, "テキスト上はマージできる"
        assert r.overlap_files == {"shared.py"}
        assert not r.conflict_files

    def test_l2_text_conflict(self, repo, line):
        r = pair(repo, line, "b", "d")
        assert r.level is Level.L2
        assert [c.path for c in r.conflict_files] == ["shared.py"]
        assert r.conflict_files[0].stages == {1, 2, 3}
        assert not r.conflict_files[0].is_structural

    def test_l3_add_add(self, repo, line):
        """add/add は型フィールドが ``CONFLICT (contents)`` になるため、
        文字列一致では L2 に誤分類される。ステージ集合で判定できていること。"""
        r = pair(repo, line, "e", "f")
        assert r.level is Level.L3
        cf = r.conflict_files[0]
        assert cf.path == "new.py"
        assert cf.stages == {2, 3}, "base ステージが無い"
        assert cf.types == ("CONFLICT (contents)",), "型は内容衝突を名乗る"
        assert cf.is_structural

    def test_l3_modify_delete(self, repo, line):
        r = pair(repo, line, "g", "h")
        assert r.level is Level.L3
        assert r.conflict_files[0].stages == {1, 3}

    def test_l3_rename_delete(self, repo, line):
        r = pair(repo, line, "g", "i")
        assert r.level is Level.L3
        assert any(c.is_structural for c in r.conflict_files)

    def test_rename_does_not_hide_overlap(self, repo, line):
        """改名検出が重複判定を素通りさせないこと（回帰）。

        `git diff --name-only` は既定で改名を検出し、新しいパスだけを
        報告する。そのままだと「削除 x 改名」のペアで変更ファイル集合が
        重ならず、L0 と誤判定して merge-tree を呼ばずに終わってしまう。
        """
        deleted = cand(repo, line, "g")
        renamed = cand(repo, line, "i")
        assert "base.py" in deleted.changed_files
        assert "base.py" in renamed.changed_files, "旧パスが消えていないこと"
        assert "renamed.py" in renamed.changed_files
        assert deleted.changed_files & renamed.changed_files == {"base.py"}


class TestSemanticWarnings:
    def test_same_function_region(self, repo, line):
        """同じ関数の別の行を触った 2 つの変更。テキストはマージできる。

        検出は **関数名の一致** で行う。k と l は同じ関数の別々の行を
        触るので変更行範囲は重ならない —— 行範囲の交差だけを見る実装では
        取りこぼす。
        """
        r = pair(repo, line, "k", "l")
        assert r.level is Level.L1, "テキスト衝突はしない"
        kinds = {w.kind for w in r.warnings}
        assert WarningKind.SAME_FUNCTION_REGION in kinds
        w = next(w for w in r.warnings if w.kind is WarningKind.SAME_FUNCTION_REGION)
        assert w.path == "shared.py"
        assert w.symbols == ("head_fn",), "関数名で特定できる"
        assert "head_fn" in w.detail

    def test_distant_regions_do_not_warn(self, repo, line):
        """別の関数を触った場合は同一関数警告を出さない（偽陽性の回帰）。"""
        r = pair(repo, line, "b", "c")
        assert WarningKind.SAME_FUNCTION_REGION not in {w.kind for w in r.warnings}

    def test_dependency_file_overlap(self, repo, line):
        r = pair(repo, line, "m", "n")
        assert r.level is Level.L1
        w = next(w for w in r.warnings if w.kind is WarningKind.DEPENDENCY_OR_CONFIG_OVERLAP)
        assert w.path == "requirements.txt"

    def test_no_function_warning_for_non_python(self, repo, line):
        """requirements.txt に対して関数境界の警告を出さない。"""
        r = pair(repo, line, "m", "n")
        assert all(
            w.kind is not WarningKind.SAME_FUNCTION_REGION
            for w in r.warnings
            if w.path == "requirements.txt"
        )


class TestLandingTrees:
    def test_landing_tree_merges_change_into_line(self, repo, line):
        c = cand(repo, line, "b")
        assert c.landing_tree
        assert not c.has_base_conflict
        assert c.changed_files == {"shared.py"}

    def test_l1_merge_keeps_both_changes(self, repo, line):
        """L1 と判定したペアが実際に両方の変更を保持していること。"""
        a, b = cand(repo, line, "b"), cand(repo, line, "c")
        merged = repo.pair_merge(line, a.landing_tree, b.landing_tree)
        assert merged.clean
        content = repo.run("cat-file", "-p", f"{merged.tree_oid}:shared.py")
        assert "head = 222" in content
        assert "tail = 333" in content


class TestPairwiseSweep:
    def test_analyze_line_omits_l0_by_default(self, repo, line):
        cands = [cand(repo, line, b) for b in ("a", "b", "c", "d")]
        pairs = interference.analyze_line(repo, line, cands)
        assert all(p.level is not Level.L0 for p in pairs)
        with_l0 = interference.analyze_line(repo, line, cands, include_l0=True)
        assert len(with_l0) > len(pairs)

    def test_stacked_pairs_are_not_levelled(self, repo, line):
        """祖先/子孫は干渉を計算しない（必ずクリーンになり誤解を招くため）。"""
        parent = cand(repo, line, "b")
        child = cand(repo, line, "d", ancestors=frozenset({"b"}))
        r = interference.analyze_pair(repo, line, parent, child)
        assert r.relation is Relation.STACKED
        assert r.level is None

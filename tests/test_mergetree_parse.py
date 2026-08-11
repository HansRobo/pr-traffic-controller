"""層1: パーサの純関数テスト。

実 git が吐いたバイト列を固定してあるので、このテストは git を必要とせず
どの環境でも走る（ローカルの git 2.34 でも可）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from analyzer.interference import classify_file, conflict_files_from
from analyzer.mergetree import InfoMessage, MergeTreeResult, parse
from analyzer.model import Level

FIXTURES = Path(__file__).parent / "fixtures" / "mergetree"


def load(name: str, *, clean: bool) -> MergeTreeResult:
    return parse((FIXTURES / f"{name}.bin").read_bytes(), clean=clean)


def levels(result: MergeTreeResult) -> dict[str, Level]:
    """パース結果を **本番と同じ経路** で等級に落とす。

    `repo=None` で呼べば git を触らずに済む（衝突箇所の中身は取れないが、
    等級はステージ集合と型だけで決まる）。分類器を 2 つ持たないために、
    テストもこの経路を通す。
    """
    return {cf.path: classify_file(cf) for cf in conflict_files_from(result)}


class TestCleanMerges:
    def test_clean_without_info_section(self):
        """情報節も空区切りレコードも無い、tree OID だけの出力。

        変更ファイルが重ならないペアではこの形になる。パーサが
        「空区切りレコードが必ずある」と仮定していると壊れる。
        """
        r = load("clean_no_info", clean=True)
        assert r.clean
        assert len(r.tree_oid) == 40
        assert r.stages == {}
        assert r.messages == ()
        assert r.conflict_paths == frozenset()
        assert levels(r) == {}

    def test_clean_landing_tree_pair(self):
        """着地tree同士のクリーンなマージ（同一ファイル・別領域 = L1）。"""
        r = load("syn_clean_automerging", clean=True)
        assert r.clean
        assert r.stages == {}
        assert r.conflict_paths == frozenset()


class TestContentConflicts:
    def test_content_conflict_has_all_stages(self):
        """ステージ 1/2/3 が揃う内容衝突。"""
        r = load("commit_form_mixed", clean=False)
        assert not r.clean
        assert r.conflict_paths == {"shared.py"}
        assert r.stages["shared.py"] == {1, 2, 3}
        # 内容衝突のみなので構造衝突ではない
        assert levels(r) == {"shared.py": Level.L2}

    def test_auto_merging_records_are_not_conflicts(self):
        """クリーンにマージされたファイルの Auto-merging を衝突と数えない。

        情報節は「衝突リスト」ではない。全レコードを衝突として扱うと、
        自動マージされただけのファイルまで衝突に数えてしまう。
        """
        r = load("commit_form_mixed", clean=False)
        auto = [m for m in r.messages if m.type == "Auto-merging"]
        assert {m.paths[0] for m in auto} == {"base.py", "shared.py"}
        # base.py は自動マージされただけなので衝突ではない
        assert "base.py" not in r.conflict_paths
        assert r.conflict_paths == {"shared.py"}

    def test_tree_form_matches_commit_form(self):
        """bare tree OID を渡した形式でも同じ構造で読める。

        ペアワイズ解析は着地tree（commit ではなく tree OID）同士を
        マージするので、この形式が壊れると解析全体が壊れる。
        """
        tree = load("tree_form_mixed", clean=False)
        commit = load("commit_form_mixed", clean=False)
        assert not tree.clean
        assert tree.conflict_paths == commit.conflict_paths
        assert tree.stages == commit.stages
        assert conflict_files_from(tree) == conflict_files_from(commit)


class TestStructuralConflicts:
    """ステージ集合による構造衝突の判定。

    型フィールドの文字列一致では add/add を検出できない（型は
    ``CONFLICT (contents)`` になる）ため、ここが分類の要になる。
    """

    def test_add_add_has_no_base_stage(self):
        r = load("syn_conflict_add_add", clean=False)
        assert r.stages["new.py"] == {2, 3}, "base(1) が無いのが add/add の印"
        assert levels(r) == {"new.py": Level.L3}
        # 型フィールドだけを見ると内容衝突に見えてしまうことの回帰テスト
        assert r.conflict_types == {"CONFLICT (contents)"}
        assert any("add/add" in m.message for m in r.messages)

    def test_modify_delete(self):
        r = load("syn_conflict_modify_delete", clean=False)
        assert r.stages["base.py"] == {1, 3}
        assert levels(r) == {"base.py": Level.L3}
        assert r.conflict_types == {"CONFLICT (modify/delete)"}

    def test_rename_delete(self):
        r = load("syn_conflict_rename_delete", clean=False)
        assert r.stages["renamed.py"] == {1, 3}
        # 改名元と改名先の両方が衝突パスに出る（情報レコードが両方を挙げる）
        assert levels(r) == {"base.py": Level.L3, "renamed.py": Level.L3}
        assert r.conflict_types == {"CONFLICT (rename/delete)"}

    def test_content_conflict_is_not_structural(self):
        r = load("syn_conflict_content", clean=False)
        assert r.stages["shared.py"] == {1, 2, 3}
        assert levels(r) == {"shared.py": Level.L2}


class TestParserRobustness:
    def test_empty_input_rejected(self):
        with pytest.raises(ValueError):
            parse(b"", clean=True)

    def test_message_trailing_newline_stripped(self):
        r = load("syn_conflict_add_add", clean=False)
        for m in r.messages:
            assert not m.message.endswith("\n")

    def test_malformed_stage_line_rejected(self):
        bad = b"a" * 40 + b"\0" + b"garbage-without-tab\0"
        with pytest.raises(ValueError):
            parse(bad, clean=False)

    @pytest.mark.parametrize(
        "path",
        [
            "dir/with space.py",
            "日本語/ファイル.py",
            "weird\nnewline.py",
            "tabs\tare\there.py",
        ],
    )
    def test_paths_with_special_characters(self, path: str):
        """`-z` ではパスはクオートされない。空白・改行・非ASCII を通す。

        ステージ行はメタ情報とパスを最初の TAB で分けるので、パス自身が
        TAB を含んでいても失われない（レコード境界は NUL であって TAB
        ではないため、曖昧さは生じない）。
        """
        raw = path.encode()
        data = (
            b"t" * 40
            + b"\0"
            + b"100644 " + b"o" * 40 + b" 1\t" + raw + b"\0"
            + b"\0"
            + b"1\0" + raw + b"\0CONFLICT (contents)\0CONFLICT (contents): x\n\0"
        )
        r = parse(data, clean=False)
        assert path in r.stages, "パスは一切改変されずに保持される"
        assert r.conflict_paths == {path}

    def test_info_message_with_multiple_paths(self):
        """rename/rename のように 1 レコードが複数パスを持つ場合。"""
        data = (
            b"t" * 40
            + b"\0\0"
            + b"3\0a.py\0b.py\0c.py\0CONFLICT (rename/rename)\0msg\n\0"
        )
        r = parse(data, clean=False)
        assert r.messages == (
            InfoMessage(paths=("a.py", "b.py", "c.py"), type="CONFLICT (rename/rename)", message="msg"),
        )
        assert r.conflict_paths == {"a.py", "b.py", "c.py"}
        # ステージを一切生成しない衝突。型で L3 に拾えること
        assert levels(r) == {p: Level.L3 for p in ("a.py", "b.py", "c.py")}


class TestConflictHunks:
    """衝突マーカーから両側の中身を取り出す。"""

    def test_two_way_markers(self):
        from analyzer.mergetree import parse_conflict_hunks

        text = "a\n<<<<<<< ours\nX1\nX2\n=======\nY1\n>>>>>>> theirs\nb\n"
        (h,) = parse_conflict_hunks(text)
        assert h.ours == ("X1", "X2")
        assert h.theirs == ("Y1",)
        assert h.start_line == 2

    def test_diff3_base_section_is_skipped(self):
        from analyzer.mergetree import parse_conflict_hunks

        text = "<<<<<<< ours\nX\n||||||| base\nB\n=======\nY\n>>>>>>> theirs\n"
        (h,) = parse_conflict_hunks(text)
        assert h.ours == ("X",) and h.theirs == ("Y",)

    def test_multiple_hunks_are_capped(self):
        from analyzer.mergetree import MAX_HUNKS_PER_FILE, parse_conflict_hunks

        one = "<<<<<<< o\nX\n=======\nY\n>>>>>>> t\n"
        assert len(parse_conflict_hunks(one * 10)) == MAX_HUNKS_PER_FILE

    def test_long_sides_are_truncated(self):
        from analyzer.mergetree import MAX_LINES_PER_SIDE, parse_conflict_hunks

        body = "\n".join(f"L{i}" for i in range(50))
        (h,) = parse_conflict_hunks(f"<<<<<<< o\n{body}\n=======\nY\n>>>>>>> t\n")
        assert len(h.ours) == MAX_LINES_PER_SIDE
        assert h.ours_truncated and not h.theirs_truncated

    def test_no_markers(self):
        from analyzer.mergetree import parse_conflict_hunks

        assert parse_conflict_hunks("plain text\nno markers\n") == ()

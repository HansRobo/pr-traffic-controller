"""ファイル・関数を軸にした変更収集のテスト。

`build` は git を `repo.run("diff", ...)` でしか呼ばないので、実 git を立てずに
フェイクで足りる（＝速いローカル層に置ける）。
"""

from __future__ import annotations

import time

import pytest

from analyzer.filechanges import MAX_HUNKS, MAX_LINES, build, parse_diff
from analyzer.model import Candidate

from .factories import make_candidate

DIFF = """diff --git a/x.py b/x.py
index 1111111..2222222 100644
--- a/x.py
+++ b/x.py
@@ -10,3 +10,4 @@ def get_args():
     a = 1
+    b = 2
     c = 3
@@ -40,2 +41,2 @@ class Config:
-    old = True
+    new = True
"""


class TestParseDiff:
    def test_splits_into_hunks_with_context(self):
        hunks = parse_diff(DIFF)
        assert len(hunks) == 2
        assert hunks[0].line == 10
        assert hunks[0].function == "get_args"
        assert hunks[1].function == "Config"

    def test_keeps_markers_and_bodies(self):
        h = parse_diff(DIFF)[0]
        assert ("+", "    b = 2") in h.lines
        assert (" ", "    a = 1") in h.lines

    def test_deletion_and_addition(self):
        h = parse_diff(DIFF)[1]
        marks = [m for m, _ in h.lines]
        assert "-" in marks and "+" in marks

    def test_headers_are_dropped(self):
        for h in parse_diff(DIFF):
            for _, body in h.lines:
                assert not body.startswith("diff --git")
                assert not body.startswith("index ")

    def test_non_function_context_has_no_function(self):
        text = "@@ -1,1 +1,2 @@ repositories:\n x\n+y\n"
        (h,) = parse_diff(text)
        assert h.function is None
        assert h.context == "repositories:"

    def test_hunk_count_is_capped(self):
        one = "@@ -1,1 +1,1 @@ def f():\n+x\n"
        assert len(parse_diff(one * (MAX_HUNKS + 5))) == MAX_HUNKS

    def test_long_hunk_is_truncated(self):
        body = "\n".join(f"+line{i}" for i in range(MAX_LINES + 10))
        (h,) = parse_diff(f"@@ -1,1 +1,1 @@ def f():\n{body}\n")
        assert len(h.lines) == MAX_LINES
        assert h.truncated

    def test_empty_diff(self):
        assert parse_diff("") == ()


class FakeRepo:
    """`build` が使う口は `run` だけ。

    `stagger` に渡した tree の diff だけを遅らせて、**完了順を投入順と逆**にする。
    """

    def __init__(self, *, stagger: str = "", hunks_for=None):
        self.stagger = stagger
        # 既定は **全候補が同じ関数を触る**（＝ shared に残る）形。そうでないと
        # 共有関数フィルタに落ちて out が空になり、順序を検証できない。
        self.hunks_for = hunks_for or (lambda path, tree: "def f():")
        self.calls: list[tuple[str, str]] = []

    def run(self, *args, check=True):
        # ("diff", "-U1", "--no-color", line_oid, landing_tree, "--", path)
        tree, path = args[4], args[6]
        self.calls.append((path, tree))
        if tree == self.stagger:
            time.sleep(0.05)
        ctx = self.hunks_for(path, tree)
        if ctx is None:
            return ""
        return f"@@ -1,1 +1,2 @@ {ctx}\n x\n+{tree}\n"


def cand(n: int, files, **kw) -> Candidate:
    """`landing_tree=None` を渡すとベース衝突の候補になる。"""
    return make_candidate(n, changed_files=frozenset(files), **kw)


class TestBuildOrdering:
    """`zip(jobs, imap(...))` の対応付け。

    ここが崩れると **PR とその hunk を静かに取り違える**。例外は出ず、画面に
    別の PR の変更が出るだけなので、順序を明示的に押さえる必要がある。
    """

    def test_pr_rows_follow_candidate_order_not_completion_order(self, monkeypatch):
        """並びが列挙順であること。

        **これ単体では退行を捕まえられない。** `imap` を `as_completed` にしても
        全候補が hunk を返す限り `raw` への append は列挙順のままで、
        入れ替わるのは中身だけだから（実際に試して確認した）。
        実効性があるのは下の `test_each_pr_keeps_its_own_hunks` の方で、
        これはその前提となる並びを固定するためにある。
        """
        monkeypatch.setenv("PR_CONFLICT_JOBS", "4")
        cands = [cand(n, ["shared.py"]) for n in (1, 2, 3, 4)]
        # 先頭の候補の diff をいちばん遅く返す
        repo = FakeRepo(stagger="t1")

        out = build(repo, "L", cands)

        assert [row["pr"] for row in out["shared.py"]] == [
            "o/r#1", "o/r#2", "o/r#3", "o/r#4",
        ]

    def test_each_pr_keeps_its_own_hunks(self, monkeypatch):
        """順序だけでなく、**中身の対応**が入れ替わらないこと。"""
        monkeypatch.setenv("PR_CONFLICT_JOBS", "4")
        cands = [cand(n, ["shared.py"]) for n in (1, 2, 3, 4)]
        repo = FakeRepo(stagger="t1")

        out = build(repo, "L", cands)

        for n, row in zip((1, 2, 3, 4), out["shared.py"]):
            body = [b for _s, b in row["hunks"][0]["lines"]]
            assert f"t{n}" in body, "PR と hunk の対応が入れ替わっている"

    def test_paths_are_sorted(self, monkeypatch):
        monkeypatch.setenv("PR_CONFLICT_JOBS", "4")
        cands = [cand(n, ["b.py", "a.py"]) for n in (1, 2)]
        out = build(FakeRepo(stagger="t1"), "L", cands)
        assert list(out) == ["a.py", "b.py"]


class TestBuildFiltering:
    def test_file_touched_by_one_pr_is_dropped(self):
        """min_prs 未満のファイルは「比較の意味が無い」ので集めない。"""
        cands = [cand(1, ["only.py"]), cand(2, ["other.py"])]
        assert build(FakeRepo(), "L", cands) == {}

    def test_min_prs_is_configurable(self):
        cands = [cand(1, ["only.py"])]
        out = build(FakeRepo(), "L", cands, min_prs=1)
        assert list(out) == ["only.py"]

    def test_base_conflict_candidates_are_excluded(self):
        """着地tree が無い候補は diff が取れないので対象外。"""
        cands = [cand(1, ["shared.py"]), cand(2, ["shared.py"], landing_tree=None)]
        assert build(FakeRepo(), "L", cands) == {}

    def test_only_shared_functions_are_kept(self):
        """別々の関数を触っているだけなら、並べても比較にならない。"""
        cands = [cand(1, ["shared.py"]), cand(2, ["shared.py"])]
        repo = FakeRepo(hunks_for=lambda path, tree: f"def only_{tree}():")
        assert build(repo, "L", cands) == {}

    def test_unnamed_hunks_are_kept_when_the_file_conflicts(self):
        """関数を特定できない hunk も、衝突しているファイルでは残す。"""
        cands = [cand(1, ["shared.py"]), cand(2, ["shared.py"])]
        repo = FakeRepo(hunks_for=lambda path, tree: "repositories:")

        assert build(repo, "L", cands) == {}, "衝突していなければ落ちる"

        out = build(repo, "L", cands, conflicted_paths=frozenset({"shared.py"}))
        assert [row["pr"] for row in out["shared.py"]] == ["o/r#1", "o/r#2"]
        assert "function" not in out["shared.py"][0]["hunks"][0]

    def test_empty_diff_is_not_recorded(self):
        cands = [cand(1, ["shared.py"]), cand(2, ["shared.py"])]
        repo = FakeRepo(hunks_for=lambda path, tree: None)
        assert build(repo, "L", cands) == {}

    def test_diff_is_requested_once_per_path_and_candidate(self):
        """候補が触っていないパスの diff は取らない。"""
        cands = [cand(1, ["a.py", "b.py"]), cand(2, ["a.py"]), cand(3, ["a.py"])]
        repo = FakeRepo()
        build(repo, "L", cands)
        # b.py は 1 件しか触らないので hot に入らず、diff も走らない
        assert sorted(repo.calls) == [("a.py", "t1"), ("a.py", "t2"), ("a.py", "t3")]

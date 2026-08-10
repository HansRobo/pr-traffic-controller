"""ファイル・関数を軸にした変更収集のテスト。"""

from __future__ import annotations

from analyzer.filechanges import MAX_HUNKS, MAX_LINES, parse_diff

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

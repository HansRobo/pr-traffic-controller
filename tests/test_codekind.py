"""コメント／実コードの見分け。

判定は保守的に倒す。少しでも実コードらしい行が混じっていれば実コード。
コメントだけだと誤って軽く見せる方が危ないため。
"""

from analyzer.codekind import classify_hunks, is_comment_only, is_doc_file


class TestLineComments:
    def test_python_comments_only(self):
        assert is_comment_only("a.py", ["# hello", "   # world", ""])

    def test_python_with_code(self):
        assert not is_comment_only("a.py", ["# hello", "x = 1"])

    def test_cpp_comments(self):
        assert is_comment_only("a.cpp", ["// note", "// more"])
        assert not is_comment_only("a.cpp", ["// note", "int x = 1;"])

    def test_yaml_comments(self):
        assert is_comment_only("conf.yaml", ["# just a note"])
        assert not is_comment_only("conf.yaml", ["key: value"])

    def test_blank_only_is_not_comment(self):
        assert not is_comment_only("a.py", ["", "   "])

    def test_unknown_language_is_treated_as_code(self):
        assert not is_comment_only("a.zzz", ["# looks like a comment"])


class TestDocstrings:
    def test_self_contained_docstring(self):
        assert is_comment_only("a.py", ['"""', "説明", '"""'])

    def test_docstring_left_open_is_code(self):
        """範囲の外に実コードが続くかもしれないので保守的に扱う。"""
        assert not is_comment_only("a.py", ['"""', "説明だけで閉じない"])

    def test_code_before_quotes_is_code(self):
        assert not is_comment_only("a.py", ['x = """', "text", '"""'])

    def test_block_comment(self):
        assert is_comment_only("a.cpp", ["/* note", "   more", "*/"])
        assert not is_comment_only("a.cpp", ["/* note", "*/", "int x;"])


class TestDocFiles:
    def test_markdown_is_documentation(self):
        assert is_doc_file("docs/README.md")
        assert is_comment_only("docs/README.md", ["# 見出し", "本文"])

    def test_python_is_not_documentation(self):
        assert not is_doc_file("a.py")


class TestClassifyHunks:
    class H:
        def __init__(self, ours, theirs):
            self.ours, self.theirs = ours, theirs

    def test_all_comment_hunks(self):
        hunks = [self.H(["# a"], ["# b"]), self.H(["# c"], ["# d"])]
        assert classify_hunks("x.py", hunks)

    def test_one_code_hunk_makes_it_code(self):
        hunks = [self.H(["# a"], ["# b"]), self.H(["x = 1"], ["x = 2"])]
        assert not classify_hunks("x.py", hunks)

    def test_either_side_with_code_counts(self):
        assert not classify_hunks("x.py", [self.H(["# a"], ["y = 2"])])

    def test_no_hunks(self):
        assert not classify_hunks("x.py", [])

    def test_doc_file_regardless_of_hunks(self):
        assert classify_hunks("CHANGELOG.md", [self.H(["anything"], ["else"])])

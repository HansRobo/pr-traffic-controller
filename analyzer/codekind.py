"""衝突している中身が「コメント・文書」か「実コード」かを見分ける。

同じ L2（テキスト衝突）でも、実コードがぶつかっているのか、コメントや
docstring だけがぶつかっているのかで、解決の難しさもリスクもまったく違う。
前者は挙動が変わりうるので設計判断が要り、後者はたいてい両方を残すか
どちらかを選べば済む。

**衝突の等級（L1〜L3）は下げない。** git がマージできないという事実は
変わらないため。代わりに直交する印として持ち、順序付けのコストと
画面表示で扱いを変える。

判定は保守的に倒す。少しでも実コードらしい行が混じっていれば
「実コード」と見なす —— コメントだけだと誤って軽く見せる方が、
逆よりずっと危ない。
"""

from __future__ import annotations

import re

#: 拡張子 -> 行コメントの開始記号
LINE_COMMENT = {
    ".py": ("#",),
    ".pyi": ("#",),
    ".sh": ("#",),
    ".bash": ("#",),
    ".yaml": ("#",),
    ".yml": ("#",),
    ".toml": ("#",),
    ".cfg": ("#",),
    ".ini": ("#",),
    ".rb": ("#",),
    ".cmake": ("#",),
    ".dockerfile": ("#",),
    ".c": ("//",),
    ".h": ("//",),
    ".cpp": ("//",),
    ".hpp": ("//",),
    ".cc": ("//",),
    ".cu": ("//",),
    ".js": ("//",),
    ".mjs": ("//",),
    ".ts": ("//",),
    ".go": ("//",),
    ".rs": ("//",),
    ".java": ("//",),
}

#: 中身が丸ごと文書であるファイル。
DOC_SUFFIXES = (".md", ".rst", ".txt", ".adoc", ".org")

#: Python の docstring 等、三重引用符の開始・終了。
_TRIPLE = re.compile(r'"""|\'\'\'')

#: `/* ... */` 形式。
_BLOCK_OPEN = "/*"
_BLOCK_CLOSE = "*/"


def is_doc_file(path: str) -> bool:
    """ファイルそのものが文書か。"""
    lower = path.lower()
    return lower.endswith(DOC_SUFFIXES)


def _suffix(path: str) -> str:
    name = path.rsplit("/", 1)[-1].lower()
    if "." not in name:
        return ""
    return "." + name.rsplit(".", 1)[-1]


def is_comment_only(path: str, lines: list[str] | tuple[str, ...]) -> bool:
    """与えられた行がコメント・空行だけで構成されているか。

    三重引用符と ``/* */`` は、その塊がこの範囲の中で開いて閉じている
    場合にかぎりコメントとして扱う。範囲の外へまたがっている場合は
    実コードの一部かもしれないので、保守的に「実コード」とする。
    """
    if not lines:
        return False

    prefixes = LINE_COMMENT.get(_suffix(path))
    if prefixes is None and not is_doc_file(path):
        # 未知の言語では判定できない。安全側に倒す。
        return False

    in_triple = False
    in_block = False
    saw_content = False

    for raw in lines:
        s = raw.strip()
        if not s:
            continue

        if in_triple:
            saw_content = True
            if _TRIPLE.search(s):
                in_triple = False
            continue
        if in_block:
            saw_content = True
            if _BLOCK_CLOSE in s:
                in_block = False
            continue

        if prefixes and s.startswith(prefixes):
            saw_content = True
            continue

        # 行の途中から始まる三重引用符・ブロックコメント
        marks = _TRIPLE.findall(s)
        if marks:
            # 行内で開いて閉じているなら、その行はコメントだけとは限らない
            before = s[: s.index(marks[0])].strip()
            if before:
                return False
            saw_content = True
            if len(marks) % 2 == 1:
                in_triple = True
            continue

        if s.startswith(_BLOCK_OPEN):
            saw_content = True
            if _BLOCK_CLOSE not in s:
                in_block = True
            continue

        if is_doc_file(path):
            # 文書ファイルの中身はすべて散文として扱う
            saw_content = True
            continue

        return False

    # 開きっぱなしなら、範囲の外に実コードが続いている可能性がある
    if in_triple or in_block:
        return False
    return saw_content


def classify_hunks(path: str, hunks) -> bool:
    """衝突した箇所すべてがコメント・文書だけかを判定する。

    `hunks` は `mergetree.ConflictHunk` の並び。両側とも見る。
    """
    if not hunks:
        return False
    if is_doc_file(path):
        return True
    for h in hunks:
        both = list(h.ours) + list(h.theirs)
        if not is_comment_only(path, both):
            return False
    return True

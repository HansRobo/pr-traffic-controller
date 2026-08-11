"""ファイル・関数を軸にした「各 PR が何をしようとしているか」。

干渉の一覧は PR のペア単位（A と B がぶつかる）で出るが、それだけでは
「このファイルのこの関数を、関係する PR がそれぞれどう変えようとして
いるのか」が読み取れない。ペアの数だけ 1 対 1 の比較が並ぶことになり、
3 件以上が同じ場所を触っているときに全体像が掴めない。

ここでは軸を反転させ、**場所（ファイル・関数）ごとに、そこを触る
すべての PR の変更を集める**。

量を抑えるため、対象は次に絞る:

* 2 件以上の PR が触るファイルだけ（1 件だけなら比較の必要がない）
* そのファイルの中でも、2 件以上の PR が触る関数の hunk だけ
  （ただしファイルに衝突がある場合は、関数が特定できない hunk も残す）
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from . import parallel
from .gitops import function_name_of

if TYPE_CHECKING:
    from .gitops import Repo
    from .model import Candidate

#: 1 つの (PR, ファイル) で保持する hunk の上限。
MAX_HUNKS = 4
#: 1 hunk で保持する行数の上限。
MAX_LINES = 16

#: hunk ヘッダから **target 側（`+`）** の行番号を取る。`gitops` 側は
#: base 側（`-`）を取るので、正規表現は共有できない（座標系が違う）。
_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@ ?(.*)$")


@dataclass(frozen=True)
class ChangeHunk:
    line: int
    context: str
    lines: tuple[tuple[str, str], ...]
    """(記号, 本文)。記号は '+' / '-' / ' '。"""

    truncated: bool = False

    @property
    def function(self) -> str | None:
        return function_name_of(self.context)


def parse_diff(text: str) -> tuple[ChangeHunk, ...]:
    """`git diff -U<n>` の出力を hunk に切り分ける。"""
    hunks: list[ChangeHunk] = []
    cur: list[tuple[str, str]] = []
    line = 0
    context = ""

    def flush() -> None:
        if not cur:
            return
        hunks.append(
            ChangeHunk(
                line=line,
                context=context,
                lines=tuple(cur[:MAX_LINES]),
                truncated=len(cur) > MAX_LINES,
            )
        )

    for raw in text.split("\n"):
        m = _HUNK_RE.match(raw)
        if m:
            flush()
            cur = []
            line = int(m.group(2))
            context = m.group(3).strip()
            continue
        if not cur and not raw:
            continue
        if raw.startswith(("diff --git", "index ", "--- ", "+++ ", "new file", "deleted file",
                           "similarity index", "rename from", "rename to", "\\ No newline")):
            continue
        if raw[:1] in ("+", "-", " "):
            cur.append((raw[0], raw[1:]))
        elif raw == "":
            cur.append((" ", ""))
    flush()
    return tuple(hunks[:MAX_HUNKS])


def build(
    repo: "Repo",
    line_oid: str,
    candidates: list["Candidate"],
    *,
    conflicted_paths: frozenset[str] = frozenset(),
    min_prs: int = 2,
) -> dict[str, list[dict]]:
    """場所ごとに、そこを触る PR の変更を集める。

    :param conflicted_paths: 衝突が起きているファイル。関数を特定できない
        hunk も残す判断に使う。
    """
    usable = [c for c in candidates if not c.has_base_conflict and c.landing_tree]

    counts: dict[str, int] = {}
    for c in usable:
        for f in c.changed_files:
            counts[f] = counts.get(f, 0) + 1
    hot = {f for f, n in counts.items() if n >= min_prs}
    if not hot:
        return {}

    # まず全部集めてから、比較の意味がある hunk だけを残す。
    # diff は (パス × 候補) の数だけ要る。互いに独立なので並列に流すが、
    # `imap` は **投入順** で返すので `raw` の並びは列挙順のまま固定される。
    jobs = [
        (path, c)
        for path in sorted(hot)
        for c in usable
        if path in c.changed_files
    ]

    def diff_hunks(job: tuple[str, "Candidate"]) -> tuple[ChangeHunk, ...]:
        path, c = job
        text = repo.run(
            "diff", "-U1", "--no-color", line_oid, c.landing_tree, "--", path, check=False
        )
        return parse_diff(text)

    raw: dict[str, list[tuple[str, tuple[ChangeHunk, ...]]]] = {}
    for (path, c), hunks in zip(jobs, parallel.imap(diff_hunks, jobs)):
        if hunks:
            raw.setdefault(path, []).append((c.id, hunks))

    out: dict[str, list[dict]] = {}
    for path, entries in raw.items():
        # その関数を触る PR が 2 件以上あるか
        fn_count: dict[str, int] = {}
        for _pr, hunks in entries:
            for fn in {h.function for h in hunks if h.function}:
                fn_count[fn] = fn_count.get(fn, 0) + 1
        shared = {fn for fn, n in fn_count.items() if n >= min_prs}
        keep_unnamed = path in conflicted_paths

        rows = []
        for pr_id, hunks in entries:
            kept = [
                h for h in hunks
                if (h.function in shared) or (h.function is None and keep_unnamed)
            ]
            if kept:
                rows.append(
                    {
                        "pr": pr_id,
                        "hunks": [
                            {
                                "line": h.line,
                                "context": h.context,
                                **({"function": h.function} if h.function else {}),
                                "lines": [[s, b] for s, b in h.lines],
                                **({"truncated": True} if h.truncated else {}),
                            }
                            for h in kept
                        ],
                    }
                )
        if len(rows) >= min_prs:
            out[path] = rows
    return out

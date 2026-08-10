"""セマンティック警告。

干渉レベルと直交する追加のフラグで、**L1 のペアにこそ価値がある**
（テキストとしてはマージできてしまうが、意味が壊れうる箇所）。

v1 では次の 2 つだけを実装する:

* 同じ関数・クラスの領域を両者が変更した
* 依存関係・設定ファイルを両者が変更した

Python の AST を使ったクロスファイルのシンボル参照追跡（「A が関数を
リネームし、B がその呼び出しを追加した」）は誤検知が多く、61 PR 分の
解決は実装量が跳ね上がるため v1 では扱わない。
"""

from __future__ import annotations

import fnmatch
from typing import TYPE_CHECKING

from .model import InterferenceWarning, WarningKind

if TYPE_CHECKING:
    from .gitops import Repo

#: 依存関係・設定ファイルの既定パターン。config.toml で上書きできる。
DEFAULT_CONFIG_PATTERNS = (
    "requirements*.txt",
    "*/requirements*.txt",
    "pyproject.toml",
    "*/pyproject.toml",
    "setup.py",
    "setup.cfg",
    "poetry.lock",
    "uv.lock",
    "Dockerfile*",
    "*/Dockerfile*",
    ".github/workflows/*",
    "*.yaml",
    "*.yml",
)

#: 関数境界の検出（`git diff -W`）が有効に働く拡張子。
#: git は拡張子から組込みの diff ドライバを選ぶので、Python 以外に広げると
#: 「関数」の概念が無いファイルで偽陽性が出る。
FUNCTION_AWARE_SUFFIXES = (".py",)


def matches_config_pattern(path: str, patterns: tuple[str, ...] = DEFAULT_CONFIG_PATTERNS) -> bool:
    return any(fnmatch.fnmatch(path, pat) for pat in patterns)


def _overlaps(a: tuple[int, int], b: tuple[int, int]) -> bool:
    return a[0] < b[1] and b[0] < a[1]


def shared_functions(hunks_a, hunks_b) -> list[str]:
    """双方が触った関数・クラスの名前。

    git の hunk ヘッダが持つ「囲っている関数」を突き合わせる。
    行範囲の交差ではなく名前の一致を見るのが要点で、こちらの方が
    精密（`-W` による範囲拡張は実運用のデータで数百行に膨らみ、
    「同じ関数」と呼べない領域まで一致扱いになっていた）。
    """
    fa = {h.function_name for h in hunks_a if h.function_name}
    fb = {h.function_name for h in hunks_b if h.function_name}
    return sorted(fa & fb)


def overlapping_line_ranges(hunks_a, hunks_b) -> list[tuple[int, int]]:
    """実際に重なった変更行の範囲。関数名が取れない場合の補助シグナル。"""
    out: list[tuple[int, int]] = []
    for ha in hunks_a:
        for hb in hunks_b:
            ra, rb = (ha.start, ha.end), (hb.start, hb.end)
            if _overlaps(ra, rb):
                out.append((max(ra[0], rb[0]), min(ra[1], rb[1])))
    return sorted(set(out))


def detect(
    repo: "Repo",
    line: str,
    tree_a: str,
    tree_b: str,
    overlap_files: frozenset[str],
    *,
    config_patterns: tuple[str, ...] = DEFAULT_CONFIG_PATTERNS,
) -> tuple[InterferenceWarning, ...]:
    """2 つの着地tree の間のセマンティック警告を検出する。

    行範囲は **どちらも統合ライン `line` を基準に取る**。こうしないと
    座標系が揃わず、範囲の交差が意味を持たない（各 PR 自身のベースで
    取った hunk 範囲同士を比べても、同じ行番号が別の場所を指す）。
    """
    warnings: list[InterferenceWarning] = []

    for path in sorted(overlap_files):
        if matches_config_pattern(path, config_patterns):
            warnings.append(
                InterferenceWarning(
                    kind=WarningKind.DEPENDENCY_OR_CONFIG_OVERLAP,
                    path=path,
                    detail="依存関係・設定ファイルを双方が変更している",
                )
            )

        if not path.endswith(FUNCTION_AWARE_SUFFIXES):
            continue

        ha = repo.changed_hunks(line, tree_a, path)
        hb = repo.changed_hunks(line, tree_b, path)

        names = shared_functions(ha, hb)
        if names:
            warnings.append(
                InterferenceWarning(
                    kind=WarningKind.SAME_FUNCTION_REGION,
                    path=path,
                    detail="同一の関数・クラスを双方が変更: " + ", ".join(names),
                    ranges=tuple(overlapping_line_ranges(ha, hb)),
                    symbols=tuple(names),
                )
            )

    return tuple(warnings)

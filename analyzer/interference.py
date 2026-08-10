"""干渉レベルの判定とペアワイズ解析。

判定の骨子::

    changed(X) = git diff --name-only <line> <landing_tree(X)>

    祖先/子孫       -> relation=stacked   （レベルは計算しない）
    どちらかベース衝突 -> relation=degraded  （ファイル重複のみ報告）
    重複ファイルなし   -> L0               （merge-tree を呼ばない）
    マージがクリーン   -> L1
    構造衝突あり      -> L3
    それ以外          -> L2

L0 と L1 は merge-tree では区別できない（クリーンはクリーンとしか返らない）
ので、変更ファイル集合が別途必要になる。その副産物として、重複ゼロの
ペアは merge-tree の呼び出しごと省ける。
"""

from __future__ import annotations

import itertools
from typing import TYPE_CHECKING, Iterable

from . import semantic
from .model import (
    Candidate,
    ConflictFile,
    Level,
    PairResult,
    Relation,
)
from .mergetree import MergeTreeResult, parse_conflict_hunks

if TYPE_CHECKING:
    from .gitops import Repo


def conflict_files_from(
    result: MergeTreeResult, repo: "Repo | None" = None
) -> tuple[ConflictFile, ...]:
    """merge-tree の結果から衝突ファイル一覧を組み立てる。

    ステージ節と、型が CONFLICT で始まる情報レコードの **和集合** を取る。
    ステージを一切生成しない衝突があるため、片方だけでは取りこぼす。
    """
    types_by_path: dict[str, set[str]] = {}
    for m in result.messages:
        if m.is_conflict:
            for p in m.paths:
                types_by_path.setdefault(p, set()).add(m.type)

    out: list[ConflictFile] = []
    for path in sorted(result.conflict_paths):
        # merge-tree が書き出した tree では、衝突ファイルに通常のマージと
        # 同じ衝突マーカーが入る。そこから両側の中身を取り出す。
        hunks: tuple = ()
        if repo is not None:
            body = repo.show(result.tree_oid, path)
            if body:
                hunks = parse_conflict_hunks(body)
        out.append(
            ConflictFile(
                path=path,
                stages=result.stages.get(path, frozenset()),
                types=tuple(sorted(types_by_path.get(path, ()))),
                hunks=hunks,
            )
        )
    return tuple(out)


def classify(result: MergeTreeResult) -> Level:
    """衝突した merge-tree 結果を L2 / L3 に分類する。

    構造衝突の判定は **ステージ番号の集合** で行う。内容衝突は
    base(1) と双方(2,3) が揃うので、欠けていれば追加・削除・改名が
    絡んでいる。型フィールドの文字列一致に頼ってはいけない ——
    add/add 衝突の型は ``CONFLICT (contents)`` であり、
    ``add/add`` という文字列は人間向けの本文にしか現れない。
    """
    if result.clean:
        raise ValueError("クリーンな結果は L2/L3 に分類できない")
    return Level.L3 if result.is_structural() else Level.L2


def analyze_pair(
    repo: "Repo",
    line: str,
    a: Candidate,
    b: Candidate,
    *,
    config_patterns: tuple[str, ...] = semantic.DEFAULT_CONFIG_PATTERNS,
    detect_semantic: bool = True,
) -> PairResult:
    """1 ペアの干渉を判定する。"""
    if a.id in b.ancestors or b.id in a.ancestors:
        return PairResult(a=a.id, b=b.id, relation=Relation.STACKED)

    if a.line != b.line:
        return PairResult(a=a.id, b=b.id, relation=Relation.INCOMPARABLE)

    overlap = a.changed_files & b.changed_files

    if a.has_base_conflict or b.has_base_conflict:
        # 着地tree が作れないので同時マージ可能性を問えない。
        # merge-base(A,B) へフォールバックしてはいけない —— 1 つの行列に
        # 2 つの異なる定義が混ざり、L2/L3 の意味が壊れる。
        return PairResult(
            a=a.id,
            b=b.id,
            relation=Relation.DEGRADED,
            level=None,
            overlap_files=overlap,
        )

    if not overlap:
        return PairResult(a=a.id, b=b.id, relation=Relation.COMPUTED, level=Level.L0)

    assert a.landing_tree and b.landing_tree
    result = repo.pair_merge(line, a.landing_tree, b.landing_tree)

    warnings = ()
    if detect_semantic:
        warnings = semantic.detect(
            repo,
            line,
            a.landing_tree,
            b.landing_tree,
            overlap,
            config_patterns=config_patterns,
        )

    if result.clean:
        return PairResult(
            a=a.id,
            b=b.id,
            relation=Relation.COMPUTED,
            level=Level.L1,
            overlap_files=overlap,
            warnings=warnings,
        )

    return PairResult(
        a=a.id,
        b=b.id,
        relation=Relation.COMPUTED,
        level=classify(result),
        conflict_files=conflict_files_from(result, repo),
        overlap_files=overlap,
        warnings=warnings,
    )


def analyze_line(
    repo: "Repo",
    line: str,
    candidates: Iterable[Candidate],
    *,
    config_patterns: tuple[str, ...] = semantic.DEFAULT_CONFIG_PATTERNS,
    detect_semantic: bool = True,
    include_l0: bool = False,
) -> list[PairResult]:
    """1 つの統合ライン内の全ペアを解析する。

    :param include_l0: L0 も結果に含めるか。既定では省く
        （「pairs に載っていない＝L0」という規約。61 PR でも大半が L0）。
    """
    cands = sorted(candidates, key=lambda c: c.id)
    out: list[PairResult] = []
    for a, b in itertools.combinations(cands, 2):
        r = analyze_pair(
            repo,
            line,
            a,
            b,
            config_patterns=config_patterns,
            detect_semantic=detect_semantic,
        )
        if r.level is Level.L0 and not include_l0:
            continue
        out.append(r)
    return out


def build_candidate(
    repo: "Repo",
    line: str,
    pr_id: str,
    head: str,
    *,
    ancestors: frozenset[str] = frozenset(),
) -> Candidate:
    """PR を統合ラインへ着地させて Candidate を作る。

    着地に失敗（＝ベース衝突）しても除外はしない。着地できない PR こそ
    交通整理の主役であり、消すと「安全」と誤読される。衝突内容を
    そのまま保持しておけば「この PR はラインに対して何が衝突しているか」
    というレポートが追加コストなしで得られる。
    """
    result = repo.landing_tree(line, head)
    if result.clean:
        tree = result.tree_oid
        return Candidate(
            id=pr_id,
            head=head,
            line=line,
            landing_tree=tree,
            changed_files=repo.changed_files(line, tree),
            ancestors=ancestors,
        )

    # ベース衝突。着地tree は信用できないので None にするが、
    # 変更ファイル集合は merge-base からの diff で代用する。
    mb = repo.merge_base(line, head)
    changed = repo.changed_files(mb, head) if mb else frozenset()
    return Candidate(
        id=pr_id,
        head=head,
        line=line,
        landing_tree=None,
        changed_files=changed,
        base_conflicts=conflict_files_from(result, repo),
        ancestors=ancestors,
    )

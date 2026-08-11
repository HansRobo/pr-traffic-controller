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
from dataclasses import replace
from typing import TYPE_CHECKING, Iterable

from . import codekind, parallel, semantic
from .model import (
    Candidate,
    ConflictFile,
    FileInterference,
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
                # チャンクごとに「コメントだけか」を判定して持たせる。
                # ファイル単位に丸めると、1 箇所だけ実コードという状況が
                # 見えなくなる。
                hunks = tuple(
                    replace(h, comment_only=codekind.is_comment_only(
                        path, list(h.ours) + list(h.theirs)))
                    for h in parse_conflict_hunks(body)
                )
        out.append(
            ConflictFile(
                path=path,
                stages=result.stages.get(path, frozenset()),
                types=tuple(sorted(types_by_path.get(path, ()))),
                hunks=hunks,
                comment_only=codekind.classify_hunks(path, hunks),
            )
        )
    return tuple(out)


def classify_file(cf: ConflictFile) -> Level:
    """衝突した 1 ファイルを L2 / L3 に分類する。

    構造衝突の判定は **ステージ番号の集合** で行う。内容衝突は
    base(1) と双方(2,3) が揃うので、欠けていれば追加・削除・改名が
    絡んでいる。型フィールドの文字列一致に頼ってはいけない ——
    add/add 衝突の型は ``CONFLICT (contents)`` であり、
    ``add/add`` という文字列は人間向けの本文にしか現れない。

    ステージを一切生成しない衝突（directory/file 等）は型で拾う。
    """
    if cf.stages:
        # ステージ集合の読み方は `ConflictFile.is_structural` が持っている。
        # ここに書き写すと、git の出力形式の理解を 2 箇所で同期することになる。
        return Level.L3 if cf.is_structural else Level.L2
    return Level.L2 if cf.types == ("CONFLICT (contents)",) else Level.L3


def build_file_levels(
    overlap: frozenset[str],
    conflicts: tuple[ConflictFile, ...],
    warnings: tuple,
) -> tuple[FileInterference, ...]:
    """重なったファイルそれぞれに等級を付ける。

    衝突していないファイルは L1（同じファイルを触ったがマージできる）。
    衝突しているファイルは、そのファイル自身のステージから L2/L3 を決める。
    """
    by_path = {c.path: c for c in conflicts}
    warn_by_path: dict[str, list] = {}
    for w in warnings:
        warn_by_path.setdefault(w.path, []).append(w)

    out: list[FileInterference] = []
    for path in sorted(overlap | set(by_path)):
        cf = by_path.get(path)
        # 衝突していないファイルは衝突由来のフィールドを持たない。
        # 省いた分は `FileInterference` の既定値とちょうど一致するので、
        # 分岐ごとにコンストラクタを書き分けない（フィールドを足したとき
        # 片方だけ直しても型エラーにならない形を避ける）。
        conflict_fields = (
            {}
            if cf is None
            else dict(
                stages=cf.stages,
                types=cf.types,
                hunks=cf.hunks,
                comment_only=cf.comment_only,
            )
        )
        out.append(
            FileInterference(
                path=path,
                level=Level.L1 if cf is None else classify_file(cf),
                warnings=tuple(warn_by_path.get(path, ())),
                **conflict_fields,
            )
        )
    return tuple(out)


def blocking_relation(a: Candidate, b: Candidate) -> Relation | None:
    """レベルを計算できない関係なら、その `Relation` を返す。計算できるなら None。

    **判定はここ 1 か所に置く。** `analyze_pair` と `_prewarm_hunks` の両方が
    「このペアは merge-tree まで進むか」を知る必要があり、条件を写すと
    片方だけ変わったときに黙って乖離する（温めが効かなくなるだけなので
    気づけない）。
    """
    if a.id in b.ancestors or b.id in a.ancestors:
        # 累積ビューでは内容が包含されるので、干渉を計算しない
        return Relation.STACKED
    if a.line != b.line:
        return Relation.INCOMPARABLE
    if a.has_base_conflict or b.has_base_conflict:
        # 着地tree が作れないので同時マージ可能性を問えない
        return Relation.DEGRADED
    return None


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
    blocked = blocking_relation(a, b)
    if blocked in (Relation.STACKED, Relation.INCOMPARABLE):
        return PairResult(a=a.id, b=b.id, relation=blocked)

    overlap = a.changed_files & b.changed_files

    if blocked is Relation.DEGRADED:
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

    conflicts = () if result.clean else conflict_files_from(result, repo)
    files = build_file_levels(overlap, conflicts, warnings)
    return PairResult(
        a=a.id,
        b=b.id,
        relation=Relation.COMPUTED,
        # ペアの等級は、ファイルごとの等級の最大。丸めた値は一覧の並べ替えや
        # 順序付けに使うだけで、内訳は files に残る。
        level=max((f.level for f in files), default=Level.L1),
        files=files,
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

    ペアは互いに独立（`Repo` を読むだけ）なので並列に流すが、**結果は
    `combinations` の順に並べる**。順序が実行ごとに変わると公開 JSON が
    無意味に揺れる。
    """
    cands = sorted(candidates, key=lambda c: c.id)
    pairs = list(itertools.combinations(cands, 2))

    # ペアの fan-out に入る前に hunk のキャッシュを温めておく。
    # `changed_hunks(line, tree, path)` は候補ごとに 1 回でよいのに
    # ペアごとに問われるので、温めないと並列度の分だけ重複計算が走る
    # （直列なら 2 回目以降がキャッシュに当たるが、並列だと同時に走る）。
    if detect_semantic:
        _prewarm_hunks(repo, line, pairs)

    def run(pair: tuple[Candidate, Candidate]) -> PairResult:
        a, b = pair
        return analyze_pair(
            repo,
            line,
            a,
            b,
            config_patterns=config_patterns,
            detect_semantic=detect_semantic,
        )

    return [
        r
        for r in parallel.imap(run, pairs)
        if include_l0 or r.level is not Level.L0
    ]


def _prewarm_hunks(
    repo: "Repo", line: str, pairs: list[tuple[Candidate, Candidate]]
) -> None:
    """ペア解析が実際に問う `(landing_tree, path)` を先に計算しておく。

    候補の変更ファイル全部ではなく **重なったファイルだけ** に絞る。
    100 ファイル触る PR でも重なるのは数件で、全部温めると逆に高くつく。

    どのペアが merge-tree まで進むかは `blocking_relation` が唯一の情報源。
    対象拡張子だけは `semantic.detect` 側が正なので、そちらを直接参照する。
    """
    needed: set[tuple[str, str]] = set()
    for a, b in pairs:
        if blocking_relation(a, b) is not None:
            continue
        for path in a.changed_files & b.changed_files:
            if path.endswith(semantic.FUNCTION_AWARE_SUFFIXES):
                assert a.landing_tree and b.landing_tree
                needed.add((a.landing_tree, path))
                needed.add((b.landing_tree, path))

    list(parallel.imap(lambda k: repo.changed_hunks(line, k[0], k[1]), sorted(needed)))


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

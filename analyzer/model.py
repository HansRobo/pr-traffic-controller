"""解析全体で使うデータ構造。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, IntEnum
from functools import cached_property


class Level(IntEnum):
    """干渉の強さ。数値が大きいほど深刻。"""

    L0 = 0
    """無干渉。変更ファイルが重ならない。"""

    L1 = 1
    """同一ファイルを触るがテキスト上はマージできる。意味的な破壊はありうる。"""

    L2 = 2
    """テキスト衝突。git が自動マージできない。"""

    L3 = 3
    """構造衝突。追加・削除・改名が絡み、解決には設計判断が要る。"""


class Relation(str, Enum):
    """ペアの関係。レベルが計算できない場合を明示的に区別する。"""

    COMPUTED = "computed"
    """通常のペア。レベルが計算されている。"""

    STACKED = "stacked"
    """祖先/子孫関係。累積ビューでは内容が包含されるため干渉を計算しない。

    計算するとつねにクリーンになり「干渉なし」という誤った安心を与える。
    """

    DEGRADED = "degraded"
    """片方がベースと衝突しており着地tree を作れない。ファイル重複のみ報告する。"""

    INCOMPARABLE = "incomparable"
    """異なる統合ラインに属する。比較する意味が無い。"""


def pair_key(a: str, b: str) -> tuple[str, str]:
    """ペアを id 2 つから引くための正規化キー。

    向きの規約はここが唯一の持ち主。`PairResult.key()` もこれに委譲する。
    規約が散らばると、`pair_map` の作り方だけ変えたときに引き側が全部
    黙って miss し、衝突ペアがコスト 0 として扱われる。
    """
    return (a, b) if a <= b else (b, a)


class WarningKind(str, Enum):
    SAME_FUNCTION_REGION = "same_function_region"
    """同じ関数・クラスの領域を両者が変更した。テキストは通るが意味が壊れうる。"""

    DEPENDENCY_OR_CONFIG_OVERLAP = "dependency_or_config_overlap"
    """依存関係や設定ファイルを両者が変更した。"""


@dataclass(frozen=True)
class ReviewNote:
    """レビューでの指摘。「どこをどう直すか」を PR 単位で持つ。

    インラインコメントは `path` / `line` を持ち、PR 全体へのレビュー本文は
    持たない。解決済みのスレッドは対象外（もう直す必要がない）。
    """

    author: str
    state: str
    """CHANGES_REQUESTED | COMMENTED | INLINE"""

    body: str
    path: str = ""
    line: int | None = None
    url: str = ""
    outdated: bool = False
    """コメント後にその箇所が変更され、行が追随できなくなったもの。"""


@dataclass(frozen=True)
class PullRequest:
    """GitHub から取得した PR のメタデータ。

    head と base は **リポジトリとブランチの組**で持つ。フォークを跨ぐ
    スタックを解くには、ブランチ名だけでは足りない。
    """

    repo: str
    number: int
    title: str
    url: str
    author: str
    head_repo: str
    head_branch: str
    head_oid: str
    base_repo: str
    base_branch: str
    is_draft: bool = False
    review_decision: str = "REVIEW_REQUIRED"
    github_mergeable: str = "UNKNOWN"
    additions: int = 0
    deletions: int = 0
    changed_files_count: int = 0
    created_at: str = ""
    updated_at: str = ""
    author_avatar_url: str = ""
    review_notes: tuple[ReviewNote, ...] = ()

    @property
    def id(self) -> str:
        return f"{self.repo}#{self.number}"

    @property
    def is_cross_repository(self) -> bool:
        return self.head_repo != self.base_repo

    @property
    def is_approved(self) -> bool:
        return self.review_decision == "APPROVED"


@dataclass(frozen=True)
class InterferenceWarning:
    kind: WarningKind
    path: str
    detail: str = ""
    ranges: tuple[tuple[int, int], ...] = ()
    symbols: tuple[str, ...] = ()
    """双方が変更した関数・クラスの名前。"""


@dataclass(frozen=True)
class ConflictFile:
    path: str
    stages: frozenset[int]
    types: tuple[str, ...] = ()
    hunks: tuple = ()
    """衝突箇所の両側の中身（mergetree.ConflictHunk）。"""

    comment_only: bool = False
    """衝突している中身がコメント・文書だけか。

    等級（L1〜L3）は下げない —— git がマージできない事実は変わらない。
    解決の難易度とリスクが違うことを示す、直交した印として持つ。
    """

    @property
    def is_structural(self) -> bool:
        return self.stages != frozenset({1, 2, 3})


@dataclass
class Candidate:
    """干渉解析の対象となる 1 件（＝1 PR）。

    GitHub のメタデータからは独立させてあるので、テストでは合成リポの
    ブランチをそのまま Candidate として扱える。
    """

    id: str
    head: str
    """head の commit OID または ref。"""

    line: str
    """所属する統合ライン。スタックした PR はルートから推移的に継承する。"""

    landing_tree: str | None = None
    """統合ラインへ着地させた結果の tree。ベース衝突時は None。"""

    changed_files: frozenset[str] = frozenset()
    base_conflicts: tuple[ConflictFile, ...] = ()
    ancestors: frozenset[str] = frozenset()
    """スタック上の祖先 PR の id。"""

    @property
    def has_base_conflict(self) -> bool:
        return self.landing_tree is None


@dataclass(frozen=True)
class FileInterference:
    """ペアの中の **1 ファイル** についての干渉。

    ペアに 1 つの等級しか持たせないと、複数のファイルに跨るときに
    最悪値へ丸められる。「16 ファイルが衝突し、19 ファイルは重なった
    だけ」でも一律 L3 になり、どこを直せばよいのかが読み取れない。
    ファイルごとに判定して持つ。
    """

    path: str
    level: Level
    stages: frozenset[int] = frozenset()
    types: tuple[str, ...] = ()
    hunks: tuple = ()
    comment_only: bool = False
    warnings: tuple[InterferenceWarning, ...] = ()

    @property
    def is_structural(self) -> bool:
        return self.level is Level.L3


@dataclass
class PairResult:
    a: str
    b: str
    relation: Relation
    level: Level | None = None
    files: tuple[FileInterference, ...] = ()
    """重なったファイルすべて。ファイルごとに等級を持つ。"""

    overlap_files: frozenset[str] = frozenset()
    warnings: tuple[InterferenceWarning, ...] = ()

    @cached_property
    def conflict_files(self) -> tuple[FileInterference, ...]:
        """実際に衝突している（git がマージできない）ファイルだけ。

        キャッシュする。順序付けの局所探索は目的関数を O(n²) 回評価し、
        その 1 回ごとに全ペアの `cost` を計算するので、素のプロパティだと
        `files` の走査が何万回も繰り返される。`PairResult` は構築後に
        `files` を書き換えないので、キャッシュしてよい。
        """
        return tuple(f for f in self.files if f.level >= Level.L2)

    @property
    def is_conflict(self) -> bool:
        return self.level is not None and self.level >= Level.L2

    @property
    def is_comment_only(self) -> bool:
        """衝突箇所がすべてコメント・文書だけか。"""
        conflicts = self.conflict_files
        return bool(conflicts) and all(c.comment_only for c in conflicts)

    def key(self) -> tuple[str, str]:
        return pair_key(self.a, self.b)


@dataclass(frozen=True)
class Skip:
    """解析から外した PR と、その理由。

    `reason` は人向けの文面で、そのまま JSON の `detail` に出る。
    **振り分けは `kind` で行うこと。** 以前は report 側が `reason` の文面を
    文字列一致で振り分け、クオートからブランチ名を取り出していたため、
    文面を 1 語変えるだけで「指定し忘れた統合ライン」の警告が黙って
    消える状態だった。構造化された情報は構造のまま運ぶ。
    """

    pr_id: str
    reason: str
    kind: str = "unresolved"
    """unresolved | unlisted_line"""

    branch: str = ""
    """`kind == "unlisted_line"` のときの、ぶら下がり先ブランチ。"""

    pr_count: int = 0
    """そのブランチに直接ぶら下がっている PR の数。"""

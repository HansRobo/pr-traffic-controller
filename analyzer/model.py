"""解析全体で使うデータ構造。"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, IntEnum


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


class WarningKind(str, Enum):
    SAME_FUNCTION_REGION = "same_function_region"
    """同じ関数・クラスの領域を両者が変更した。テキストは通るが意味が壊れうる。"""

    DEPENDENCY_OR_CONFIG_OVERLAP = "dependency_or_config_overlap"
    """依存関係や設定ファイルを両者が変更した。"""


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


@dataclass
class PairResult:
    a: str
    b: str
    relation: Relation
    level: Level | None = None
    conflict_files: tuple[ConflictFile, ...] = ()
    overlap_files: frozenset[str] = frozenset()
    warnings: tuple[InterferenceWarning, ...] = ()

    @property
    def is_conflict(self) -> bool:
        return self.level is not None and self.level >= Level.L2

    def key(self) -> tuple[str, str]:
        return (self.a, self.b) if self.a <= self.b else (self.b, self.a)


@dataclass
class LineAnalysis:
    """1 つの統合ラインについての解析結果。"""

    line: str
    candidates: list[Candidate] = field(default_factory=list)
    pairs: list[PairResult] = field(default_factory=list)

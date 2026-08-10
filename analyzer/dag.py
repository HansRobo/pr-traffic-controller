"""スタック DAG の構築。

ノードは **`(リポジトリ, ブランチ)` の組**であり、ブランチ名だけではない。
これが要点で、リポジトリを跨ぐスタック鎖はノードキーにリポジトリを
含めるだけで自然に連結する（特別扱いのコードは要らない）。

鎖の例::

    upstream/repo:main
      ^-- upstream#10 (head: upstream/repo:feature-a)
            ^-- upstream#11 (head: contributor/repo:feature-b)
                  ^-- contributor#2 (head: contributor/repo:feature-c)

upstream#11 の head ノードと contributor#2 の base ノードが同一キーに
なるため、辺が繋がる。

扱うべき異常系（いずれも実運用で観測されたもの）:

* **重複 head** —— 同一コミットが 2 つの PR の head になっている。
  ノードから複数の base 辺が出る。マージも除外もせず警告する。
* **親 PR 不在** —— base ブランチは存在するが対応する PR が無い。
  ルート候補として扱い、統合ラインを git から推定する。
* **循環** —— 通常は起きないが、起きても解析を止めない。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .model import PullRequest

NodeKey = str


def node_key(repo: str, branch: str) -> NodeKey:
    return f"{repo}:{branch}"


@dataclass(frozen=True)
class GraphWarning:
    kind: str
    severity: str
    subjects: tuple[str, ...]
    detail: str


@dataclass
class Resolution:
    """1 つの PR についてのスタック上の位置づけ。"""

    pr_id: str
    root_node: NodeKey
    line: str | None
    """解決した統合ライン。推定できなければ None。"""

    resolution: str
    """resolved | inferred | ambiguous | cyclic"""

    ancestors: tuple[str, ...] = ()
    """ルートに近い順に並んだ祖先 PR の id。"""

    @property
    def depth(self) -> int:
        return len(self.ancestors)


@dataclass
class StackGraph:
    prs: dict[str, PullRequest]
    head_index: dict[NodeKey, list[str]] = field(default_factory=dict)
    resolutions: dict[str, Resolution] = field(default_factory=dict)
    warnings: list[GraphWarning] = field(default_factory=list)

    def base_node(self, pr_id: str) -> NodeKey:
        p = self.prs[pr_id]
        return node_key(p.base_repo, p.base_branch)

    def head_node(self, pr_id: str) -> NodeKey:
        p = self.prs[pr_id]
        return node_key(p.head_repo, p.head_branch)

    def children_of(self, pr_id: str) -> list[str]:
        """この PR の head を base としている PR（＝直接の子）。"""
        head = self.head_node(pr_id)
        return sorted(
            other
            for other, p in self.prs.items()
            if other != pr_id and node_key(p.base_repo, p.base_branch) == head
        )

    def descendants_of(self, pr_id: str) -> set[str]:
        """推移的な子孫。`blocks` 指標の根拠になる。"""
        seen: set[str] = set()
        stack = list(self.children_of(pr_id))
        while stack:
            cur = stack.pop()
            if cur in seen:
                continue
            seen.add(cur)
            stack.extend(self.children_of(cur))
        return seen

    def line_of(self, pr_id: str) -> str | None:
        r = self.resolutions.get(pr_id)
        return r.line if r else None


def build(
    prs: list[PullRequest],
    integration_lines: dict[str, str],
    *,
    infer_line: "callable | None" = None,
) -> StackGraph:
    """PR 群からスタック DAG を構築し、各 PR の統合ラインを解決する。

    :param integration_lines: ノードキー -> ライン id。
        例 ``{"upstream/repo:main": "main"}``
    :param infer_line: 親 PR 不在のルートについてラインを推定する関数。
        ``(node_key) -> line_id | None``。省略時は推定しない。
    """
    graph = StackGraph(prs={p.id: p for p in prs})

    for pr_id in graph.prs:
        graph.head_index.setdefault(graph.head_node(pr_id), []).append(pr_id)

    for node, owners in sorted(graph.head_index.items()):
        if len(owners) > 1:
            graph.warnings.append(
                GraphWarning(
                    kind="duplicate_pr_head",
                    severity="warn",
                    subjects=tuple(sorted(owners)),
                    detail=(
                        f"同一の head ノード {node} を {len(owners)} 件の PR が共有している。"
                        "どちらかをクローズする掃除タスクの候補。"
                    ),
                )
            )

    for pr_id in sorted(graph.prs):
        graph.resolutions[pr_id] = _resolve(graph, pr_id, integration_lines, infer_line)

    return graph


def _resolve(
    graph: StackGraph,
    pr_id: str,
    integration_lines: dict[str, str],
    infer_line,
) -> Resolution:
    """base 辺を辿ってルートと統合ラインを決める。

    統合ラインは **ルートから推移的に継承する**。PR の base 欄を直接見ると、
    他の PR のブランチにスタックした PR がどのラインにも属さないことに
    なってしまい、順序推奨から丸ごと消える。
    """
    ancestors: list[str] = []
    seen_prs = {pr_id}
    current = graph.base_node(pr_id)

    while True:
        if current in integration_lines:
            return Resolution(
                pr_id=pr_id,
                root_node=current,
                line=integration_lines[current],
                resolution="resolved",
                ancestors=tuple(reversed(ancestors)),
            )

        owners = graph.head_index.get(current, [])
        # 重複 head の場合は決定的に選ぶ（警告は build 側で出している）
        parent = sorted(owners)[0] if owners else None

        if parent is None:
            # 親 PR が存在しないルート。git から所属ラインを推定する。
            line = infer_line(current) if infer_line else None
            if line is None:
                graph.warnings.append(
                    GraphWarning(
                        kind="orphan_base_branch",
                        severity="info",
                        subjects=(pr_id,),
                        detail=(
                            f"base ノード {current} に対応するオープン PR が無く、"
                            "統合ラインも推定できなかった。"
                        ),
                    )
                )
            else:
                graph.warnings.append(
                    GraphWarning(
                        kind="orphan_base_branch",
                        severity="info",
                        subjects=(pr_id,),
                        detail=(
                            f"base ノード {current} に対応するオープン PR が無い。"
                            f"ライン {line} と推定した。"
                        ),
                    )
                )
            return Resolution(
                pr_id=pr_id,
                root_node=current,
                line=line,
                resolution="inferred" if line else "ambiguous",
                ancestors=tuple(reversed(ancestors)),
            )

        if parent in seen_prs:
            graph.warnings.append(
                GraphWarning(
                    kind="cycle_detected",
                    severity="warn",
                    subjects=tuple(sorted(seen_prs)),
                    detail=f"{parent} を含む循環を検出したため、そこで辿るのを打ち切った。",
                )
            )
            return Resolution(
                pr_id=pr_id,
                root_node=current,
                line=None,
                resolution="cyclic",
                ancestors=tuple(reversed(ancestors)),
            )

        seen_prs.add(parent)
        ancestors.append(parent)
        current = graph.base_node(parent)

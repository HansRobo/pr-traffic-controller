"""マージ順の推奨。

**定式化** —— ハード制約付きの線形順序付け問題（LOP）。

合意形成のために、まず何を最適化しているのかを正直に述べる:
*衝突するペアは、どちらの順で流しても誰かが必ず解決する。順序が決める
のは「誰が払うか」と「支払い総額」だけである。*

    min_π  Σ_{π(p)<π(q)}  W(p→q)

    W(p→q) = sev(level) · units · (1 + γ·approved(q) + δ·size(q))
           + μ · urgency(q)

`γ·approved(q)` は「後回しにされた PR が Approve 済みなら、rebase で
レビューが無効化される」というコスト。これにより **「Approve 優先」を
後付けのボーナスではなくコストモデルから導出できる**（説明可能性に効く）。

**解法** —— 干渉グラフの連結成分に分解し、各クラスタを厳密に解く。
クラスタが小さければ部分集合ビットマスク DP で LOP の厳密最適解が
得られるので、「これは最適解です」と言い切れる。クラスタ間には衝突項が
存在しないので、並行に流してよい。
"""

from __future__ import annotations

import datetime as _dt
import itertools
import math
from array import array
from dataclasses import dataclass, field

from .dag import StackGraph
from .model import Candidate, Level, PairResult, pair_key

#: 干渉レベルごとの解決コスト係数。
SEVERITY = {
    Level.L0: 0.0,
    Level.L1: 0.15,
    Level.L2: 1.0,
    Level.L3: 2.5,
}

#: セマンティック警告が付いたペアの倍率。
SEMANTIC_MULTIPLIER = 1.5

#: 衝突箇所がコメント・文書だけのときの倍率。
#: git はマージできないので衝突は衝突だが、解決は「両方残す」か
#: 「どちらかを選ぶ」で済むことがほとんどで、実コードの衝突とは
#: 負担がまるで違う。等級は下げず、コストだけ下げる。
COMMENT_ONLY_MULTIPLIER = 0.2

#: 厳密 DP を使うクラスタサイズの上限。これを超えたら貪欲＋局所探索。
#: 2^18 × 18 の倍精度配列（約 38MB）と同数の遷移で、数秒で終わる。
EXACT_DP_LIMIT = 18

#: クラスタを繋ぐ辺とみなす干渉レベルの下限。
#:
#: 既定を L2 にしているのは、**L1（同一ファイル・別領域）はテキスト上
#: マージできるので順序を強制しない**ため。L1 で繋ぐと実運用のデータでは
#: 巨大な単一クラスタになり「順序を議論すべき単位」として機能しない
#: （L2 以上に絞ると意味のある大きさに分かれる）。L1 のコストは目的関数側で
#: 引き続き効くので、無視されるわけではない。
CLUSTER_EDGE_LEVEL = Level.L2


@dataclass(frozen=True)
class Weights:
    """プリセットごとの重み。"""

    gamma_approved: float = 1.0
    """後回しにされた Approve 済み PR への追加コスト。"""

    delta_size: float = 0.3
    """規模の寄与。規模は「後にすると再作業が巨大」としてここに入る。
    優先度そのものには入れない（大きい PR が偉いわけではない）。"""

    mu_wait: float = 1.0
    """待たせるコストの全体倍率。0 にすると純粋な衝突最小化になる。"""

    w_approved: float = 5.0
    w_stale: float = 2.0
    w_draft: float = 3.0


#: キーがプリセット名。`Weights` 自身は名前を持たない（辞書のキーと
#: 二重に持つと、片方だけ変えたときにどちらが正か分からなくなる）。
PRESETS = {
    "balanced": Weights(),
    "approve-first": Weights(w_approved=20.0, mu_wait=2.0),
    "least-conflict": Weights(mu_wait=0.0),
}


@dataclass
class PRContext:
    """順序付けに必要な 1 PR 分の情報。"""

    id: str
    approved: bool = False
    draft: bool = False
    days_stale: float = 0.0
    size: float = 0.0
    """log 正規化した変更規模。"""
    has_base_conflict: bool = False
    blocks: int = 0
    """スタック上の子孫数（ハード制約で待たされる件数）。"""


@dataclass
class Cluster:
    id: str
    members: list[str]
    internal_pairs: int = 0


@dataclass
class Metrics:
    regret: float = 0.0
    blocks: int = 0
    blast_radius: int = 0
    rebase_load: float = 0.0


@dataclass
class LinePlan:
    clusters: list[Cluster] = field(default_factory=list)
    independent: list[str] = field(default_factory=list)
    undetermined: list[str] = field(default_factory=list)
    """干渉が判定できなかった PR（ベース衝突で着地tree を作れないもの）。

    **「独立」と一緒にしてはいけない。** 着地tree が無いと全ペアが
    degraded になり、衝突辺が 1 本も立たないので、素朴にクラスタ分解すると
    「誰とも干渉しない＝独立」に見えてしまう。実際は干渉の有無が
    分かっていないだけで、rebase して初めて判定できる。
    """
    presets: dict[str, dict] = field(default_factory=dict)
    metrics: dict[str, Metrics] = field(default_factory=dict)
    order_sensitivity: dict = field(default_factory=dict)


# --- 重み -------------------------------------------------------------


def pair_units(pair: PairResult) -> float:
    """衝突の「量」。衝突ファイル数を単位として使う。

    ハンク数の方が精密だが、merge-tree はハンク数を直接は返さない。
    ファイル数は安定して取れ、順序の比較には十分な解像度がある。
    """
    if pair.level is None or pair.level < Level.L1:
        return 0.0
    # ここまで来た時点で L1 以上が確定している。衝突ファイルが 0 件でも
    # （L1 は衝突しないので普通に起きる）「量」は最低 1 とする。
    return float(max(len(pair.conflict_files), 1))


def urgency(ctx: PRContext, w: Weights) -> float:
    u = 0.0
    u += w.w_approved if ctx.approved else 0.0
    u += w.w_stale * min(ctx.days_stale / 30.0, 2.0)
    if ctx.draft:
        u -= w.w_draft
    if ctx.has_base_conflict:
        # 今すぐは流せないので、先頭に置いても意味がない
        u -= 1.0
    return u


def cost(p: PRContext, q: PRContext, pair: PairResult | None, w: Weights) -> float:
    """p を先、q を後にしたときのコスト。"""
    conflict = 0.0
    if pair is not None and pair.level is not None and pair.level >= Level.L1:
        sev = SEVERITY[pair.level]
        if pair.warnings:
            sev *= SEMANTIC_MULTIPLIER
        if pair.is_comment_only:
            sev *= COMMENT_ONLY_MULTIPLIER
        conflict = (
            sev
            * pair_units(pair)
            * (1.0 + w.gamma_approved * (1.0 if q.approved else 0.0) + w.delta_size * q.size)
        )
    return conflict + w.mu_wait * urgency(q, w)


# --- クラスタ分解 ------------------------------------------------------


def build_clusters(
    ids: list[str],
    pairs: list[PairResult],
    *,
    edge_level: Level = CLUSTER_EDGE_LEVEL,
) -> tuple[list[Cluster], list[str]]:
    """**意図しない干渉**の連結成分に分解する。

    辺は衝突ペアだけで張る。**スタックの親子辺は使わない**ので、
    `StackGraph` はここでは受け取らない（受け取ると「スタックも考慮して
    いる」と誤読される）。
    スタックは作者が意図して積んだ依存であり、順序はすでに決まっている。
    これを連結成分に混ぜると、衝突が 1 件も無いスタック鎖が
    「順序を議論すべきクラスタ」として現れてしまう —— 議論すべきことは
    何も無いのに。

    スタックの順序制約は `_predecessors` がハード制約として別途扱うので、
    クラスタから外しても順序が壊れることはない。

    連結成分が「調整が要る単位」であり、成分をまたぐ PR に
    意図しない干渉は無い。
    """
    parent = {i: i for i in ids}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    idset = set(ids)
    edge_count: dict[str, int] = {}
    for p in pairs:
        if p.a not in idset or p.b not in idset:
            continue
        if p.level is not None and p.level >= edge_level:
            union(p.a, p.b)

    groups: dict[str, list[str]] = {}
    for i in ids:
        groups.setdefault(find(i), []).append(i)

    for p in pairs:
        if p.a in idset and p.b in idset and p.level is not None and p.level >= edge_level:
            root = find(p.a)
            edge_count[root] = edge_count.get(root, 0) + 1

    clusters: list[Cluster] = []
    independent: list[str] = []
    for n, (root, members) in enumerate(sorted(groups.items(), key=lambda kv: -len(kv[1]))):
        if len(members) == 1:
            independent.append(members[0])
        else:
            clusters.append(
                Cluster(
                    id=f"c{n + 1}",
                    members=sorted(members),
                    internal_pairs=edge_count.get(root, 0),
                )
            )
    return clusters, sorted(independent)


# --- 順序付け ---------------------------------------------------------


def _predecessors(ids: list[str], graph: StackGraph | None) -> dict[str, set[str]]:
    """ハード制約: 親 PR は子 PR より必ず先。"""
    idset = set(ids)
    preds = {i: set() for i in ids}
    if graph is None:
        return preds
    for i in ids:
        res = graph.resolutions.get(i)
        if res:
            preds[i] = {a for a in res.ancestors if a in idset}
    return preds


def order_cluster(
    members: list[str],
    ctx: dict[str, PRContext],
    pair_map: dict[tuple[str, str], PairResult],
    preds: dict[str, set[str]],
    w: Weights,
) -> tuple[list[str], bool]:
    """クラスタ内を順序付ける。返り値は (順序, 厳密最適か)。"""
    n = len(members)
    if n <= 1:
        return list(members), True
    if n > EXACT_DP_LIMIT:
        return _greedy_with_local_search(members, ctx, pair_map, preds, w), False

    idx = {m: i for i, m in enumerate(members)}
    pred_mask = [0] * n
    for m, ps in preds.items():
        if m in idx:
            for p in ps:
                if p in idx:
                    pred_mask[idx[m]] |= 1 << idx[p]

    # W[i][j] = i を先、j を後にしたときのコスト
    W = [[0.0] * n for _ in range(n)]
    for i, j in itertools.permutations(range(n), 2):
        key = pair_key(members[i], members[j])
        W[i][j] = cost(ctx[members[i]], ctx[members[j]], pair_map.get(key), w)

    # add_cost[S*n + j] = 集合 S をすべて先に置いたとき、j を次に置くコスト。
    # Python のリストだと float オブジェクトのオーバーヘッドで数百 MB に
    # なるため、倍精度の平坦な array を使う（n=18 で約 38MB）。
    size = 1 << n
    INF = float("inf")
    dp = array("d", [INF]) * size
    choice = array("i", [-1]) * size
    dp[0] = 0.0

    # 部分集合ごとの追加コストは lowbit の漸化式で増分計算する
    add_cost = array("d", bytes(8 * size * n))
    for S in range(1, size):
        low = S & -S
        li = low.bit_length() - 1
        rest = (S ^ low) * n
        base_idx = S * n
        for j in range(n):
            add_cost[base_idx + j] = add_cost[rest + j] + W[li][j]

    for S in range(size):
        cur = dp[S]
        if cur == INF:
            continue
        base_idx = S * n
        for j in range(n):
            bit = 1 << j
            if S & bit:
                continue
            if pred_mask[j] & ~S:
                continue  # 先行制約を満たさない
            nxt = S | bit
            c = cur + add_cost[base_idx + j]
            if c < dp[nxt]:
                dp[nxt] = c
                choice[nxt] = j

    full = size - 1
    if dp[full] == INF:
        # 先行制約が矛盾している（循環など）。貪欲に逃がす。
        return _greedy_with_local_search(members, ctx, pair_map, preds, w), False

    seq: list[str] = []
    S = full
    while S:
        j = choice[S]
        seq.append(members[j])
        S ^= 1 << j
    seq.reverse()
    return seq, True


def _greedy_with_local_search(
    members: list[str],
    ctx: dict[str, PRContext],
    pair_map: dict[tuple[str, str], PairResult],
    preds: dict[str, set[str]],
    w: Weights,
    *,
    max_passes: int = 20,
) -> list[str]:
    """厳密 DP が使えない大きさのときのフォールバック。

    貪欲に並べたあと、先行制約を壊さない範囲での挿入近傍で改善する。
    """
    remaining = set(members)
    seq: list[str] = []
    while remaining:
        ready = [m for m in sorted(remaining) if not (preds[m] & remaining)]
        if not ready:  # 循環。決定的に打ち切る
            ready = sorted(remaining)
        best = min(
            ready,
            key=lambda m: sum(
                cost(ctx[m], ctx[o], pair_map.get(pair_key(m, o)), w)
                for o in remaining
                if o != m
            ),
        )
        seq.append(best)
        remaining.discard(best)

    def valid(order: list[str]) -> bool:
        seen: set[str] = set()
        for m in order:
            if preds[m] - seen:
                return False
            seen.add(m)
        return True

    best_cost = total_cost(seq, ctx, pair_map, w)
    for _ in range(max_passes):
        improved = False
        for i in range(len(seq)):
            for j in range(len(seq)):
                if i == j:
                    continue
                trial = seq[:i] + seq[i + 1 :]
                trial.insert(j, seq[i])
                if not valid(trial):
                    continue
                c = total_cost(trial, ctx, pair_map, w)
                if c < best_cost - 1e-9:
                    seq, best_cost, improved = trial, c, True
        if not improved:
            break
    return seq


def enforce_predecessors(seq: list[str], preds: dict[str, set[str]]) -> list[str]:
    """先行制約を満たすように並べ直す（元の並びはできるだけ保つ）。

    クラスタは意図しない干渉だけで作るので、スタックの親子が別々の
    グループに分かれることがある。グループを連結しただけでは親が子より
    後ろに来うるため、最後に必ずここを通す。

    元の順序をタイブレークに使う Kahn 法なので、制約に反しない限り
    入力の並びは変わらない。
    """
    priority = {pid: i for i, pid in enumerate(seq)}
    remaining = {pid: {a for a in preds.get(pid, set()) if a in priority} for pid in seq}
    out: list[str] = []
    placed: set[str] = set()

    while remaining:
        ready = [pid for pid, need in remaining.items() if not (need - placed)]
        if not ready:  # 循環。決定的に打ち切る
            ready = [min(remaining, key=lambda x: priority[x])]
        pick = min(ready, key=lambda x: priority[x])
        out.append(pick)
        placed.add(pick)
        del remaining[pick]
    return out


def total_cost(
    seq: list[str],
    ctx: dict[str, PRContext],
    pair_map: dict[tuple[str, str], PairResult],
    w: Weights,
) -> float:
    total = 0.0
    for i, p in enumerate(seq):
        for q in seq[i + 1 :]:
            total += cost(ctx[p], ctx[q], pair_map.get(pair_key(p, q)), w)
    return total


#: プリセット比較用の固定の物差し。
#: 各プリセットは重みが違うので `total_cost` の値同士は比較できない。
#: 「どのプリセットを選ぶと衝突解決の負担が増えるか」を同じ尺度で
#: 見るために、**常に balanced の重み**で衝突項だけを測る。
_REFERENCE = Weights(mu_wait=0.0)


def conflict_cost(
    seq: list[str],
    ctx: dict[str, PRContext],
    pair_map: dict[tuple[str, str], PairResult],
) -> float:
    """順序 `seq` が生む衝突解決コストの総額（プリセット間で比較可能）。"""
    return total_cost(seq, ctx, pair_map, _REFERENCE)


# --- 影響度指標 -------------------------------------------------------


def compute_metrics(
    ids: list[str],
    ctx: dict[str, PRContext],
    pair_map: dict[tuple[str, str], PairResult],
    w: Weights,
) -> dict[str, Metrics]:
    out: dict[str, Metrics] = {}
    for p in ids:
        m = Metrics(blocks=ctx[p].blocks)
        for q in ids:
            if p == q:
                continue
            pair = pair_map.get(pair_key(p, q))
            if pair is None or pair.level is None or pair.level < Level.L1:
                continue
            m.blast_radius += 1
            m.regret += cost(ctx[p], ctx[q], pair, w) - cost(ctx[q], ctx[p], pair, w)
            load = SEVERITY[pair.level] * pair_units(pair) * (1.0 + ctx[q].size)
            if pair.is_comment_only:
                load *= COMMENT_ONLY_MULTIPLIER
            m.rebase_load += load
        # スタックの子孫は「意図した依存」なので、意図しない干渉の
        # 広がりを測る blast_radius には足さない（blocks が別途ある）。
        out[p] = m
    return out


def plan_line(
    candidates: list[Candidate],
    pairs: list[PairResult],
    graph: StackGraph,
) -> LinePlan:
    """1 つの統合ラインについて、クラスタ分解と各プリセットの順序を作る。"""
    ids = sorted(c.id for c in candidates)
    by_id = {c.id: c for c in candidates}
    now = _dt.datetime.now(_dt.timezone.utc)

    ctx: dict[str, PRContext] = {}
    for i in ids:
        pr = graph.prs.get(i)
        stale = 0.0
        if pr and pr.updated_at:
            try:
                stale = (
                    now - _dt.datetime.fromisoformat(pr.updated_at.replace("Z", "+00:00"))
                ).days
            except ValueError:
                stale = 0.0
        churn = (pr.additions + pr.deletions) if pr else 0
        ctx[i] = PRContext(
            id=i,
            approved=bool(pr and pr.is_approved),
            draft=bool(pr and pr.is_draft),
            days_stale=float(stale),
            size=math.log10(churn + 10) - 1.0,
            has_base_conflict=by_id[i].has_base_conflict,
            blocks=len(graph.descendants_of(i) & set(ids)),
        )

    pair_map: dict[tuple[str, str], PairResult] = {p.key(): p for p in pairs}
    clusters, unclustered = build_clusters(ids, pairs)
    preds = _predecessors(ids, graph)

    # クラスタに属さないものを「独立」と「判定不能」に分ける。
    # ベース衝突の PR は着地tree が無く、干渉を計算できていない。
    undetermined = [i for i in unclustered if by_id[i].has_base_conflict]
    independent = [i for i in unclustered if not by_id[i].has_base_conflict]

    plan = LinePlan(
        clusters=clusters,
        independent=independent,
        undetermined=undetermined,
    )

    for name, w in PRESETS.items():
        seq: list[str] = []
        exact = True
        cluster_orders: dict[str, list[str]] = {}
        for c in clusters:
            o, ok = order_cluster(c.members, ctx, pair_map, preds, w)
            cluster_orders[c.id] = o
            exact = exact and ok
            seq.extend(o)
        # クラスタ間には衝突項が無いので、待たせるコストだけで並べる。
        # 判定不能なものは後ろに置く（rebase しないとそもそも流せない）。
        seq.extend(sorted(independent, key=lambda m: -urgency(ctx[m], w)))
        seq.extend(sorted(undetermined, key=lambda m: -urgency(ctx[m], w)))
        # クラスタをまたぐスタック親子があるので、最後に必ず制約を通す
        seq = enforce_predecessors(seq, preds)
        plan.presets[name] = {
            "order": seq,
            "optimal": exact,
            "method": "exact_dp" if exact else "greedy_local_search",
            "cluster_orders": cluster_orders,
            # 自プリセットの重みでの目的関数値（プリセット間で比較不可）
            "objective": round(total_cost(seq, ctx, pair_map, w), 3),
            # 固定の物差しで測った衝突解決コスト（プリセット間で比較可能）
            "conflict_cost": round(conflict_cost(seq, ctx, pair_map), 3),
        }

    plan.metrics = compute_metrics(ids, ctx, pair_map, PRESETS["balanced"])
    return plan

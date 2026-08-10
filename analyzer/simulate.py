"""推奨順の逐次マージシミュレーション。

ペアワイズの干渉行列は**安価な代理モデル**であって、累積ツリー効果を
捉えない。A と B が個別には C と衝突しなくても、A+B を適用した後の
ツリーと C が衝突することは実際に起きる。

そこで **最終推奨と代替プリセットだけ**を実際に流して検証する。
全順序を総当たりでシミュレーションすることはできない —— 1 本の検証に
O(n) 回の merge-tree が要るので、候補を増やすと桁が変わる。

得られるのは「この順で流すと **N 番目の #xxx が model.py で衝突する**」
という実証付きの記述で、これが交通整理の説得力の源になる。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from . import parallel
from .interference import conflict_files_from
from .mergetree import MergeTreeError
from .model import Candidate, ConflictFile

if TYPE_CHECKING:
    from .gitops import Repo


@dataclass
class Step:
    index: int
    pr_id: str
    result: str
    """clean | conflict | error | skipped"""
    conflict_files: tuple[ConflictFile, ...] = ()
    detail: str = ""


@dataclass
class Simulation:
    steps: list[Step] = field(default_factory=list)
    merged: int = 0
    conflicted: int = 0

    @property
    def first_conflict(self) -> Step | None:
        return next((s for s in self.steps if s.result == "conflict"), None)


def simulate(
    repo: "Repo",
    line_oid: str,
    order: list[str],
    candidates: dict[str, Candidate],
    *,
    stop_after_conflicts: int | None = None,
) -> Simulation:
    """順序どおりに 1 件ずつ統合ラインへ積み上げる。

    累積は **tree OID のまま持ち回す**（合成コミットは作らない）。
    `merge-tree` は tree を `ours` に取れるので commit にする必要が無く、
    挟めば clean なステップごとに git プロセスが 1 本増えるだけである。

    そのため **`--merge-base` を元のライン先端に明示ピン留めすることが必須**
    になる（`Repo.merge_tree` が常にそうする）。累積 tree には歴史が無いので、
    git にマージベースを推論させると静かに誤った結果を返す。

    衝突したステップはスキップして続行する。そこで打ち切ると
    「この順序で何件流せるか」が分からなくなるため。
    """
    sim = Simulation()
    acc = line_oid

    for i, pr_id in enumerate(order, start=1):
        cand = candidates.get(pr_id)
        if cand is None or cand.has_base_conflict:
            sim.steps.append(
                Step(
                    index=i,
                    pr_id=pr_id,
                    result="skipped",
                    detail="ベース衝突のため、まず rebase が必要",
                )
            )
            continue

        try:
            result = repo.merge_tree(merge_base=line_oid, ours=acc, theirs=cand.landing_tree)
        except MergeTreeError as exc:
            sim.steps.append(Step(index=i, pr_id=pr_id, result="error", detail=str(exc)))
            continue

        if result.clean:
            acc = result.tree_oid
            sim.merged += 1
            sim.steps.append(Step(index=i, pr_id=pr_id, result="clean"))
        else:
            sim.conflicted += 1
            sim.steps.append(
                Step(
                    index=i,
                    pr_id=pr_id,
                    result="conflict",
                    conflict_files=conflict_files_from(result),
                )
            )
            if stop_after_conflicts is not None and sim.conflicted >= stop_after_conflicts:
                break

    return sim


def compare_with_matrix(sim: Simulation, predicted_conflicts: set[str]) -> dict:
    """代理モデルの的中率。

    「予測 vs 実測」を出すこと自体がツールの信頼性の指標になる。
    行列に無かった衝突が逐次で出たなら、それは累積ツリー効果である。
    """
    actual = {s.pr_id for s in sim.steps if s.result == "conflict"}
    return {
        "predicted_conflicting": sorted(predicted_conflicts),
        "actual_conflicting": sorted(actual),
        "matched": len(actual & predicted_conflicts),
        "unpredicted": sorted(actual - predicted_conflicts),
        "not_realised": sorted(predicted_conflicts - actual),
    }


def _topological(items, preds: dict[str, set[str]]) -> list[str]:
    """先行制約を保った決定的な順序。循環があっても止まらない。"""
    pool = list(items)
    remaining = set(pool)
    out: list[str] = []
    placed: set[str] = set()
    while remaining:
        ready = sorted(p for p in remaining if not (preds.get(p, set()) & remaining))
        if not ready:  # 循環。決定的に打ち切る
            ready = [sorted(remaining)[0]]
        for p in ready:
            out.append(p)
            placed.add(p)
            remaining.discard(p)
    return out


def greedy_max_landing(
    repo: "Repo",
    line_oid: str,
    candidates: dict[str, Candidate],
    pool: list[str],
    predecessors: dict[str, set[str]] | None = None,
    *,
    tie_break: "callable | None" = None,
) -> list[str]:
    """clean にマージできる件数を最大化する順序を貪欲に作る。

    **これは他のプリセットとは別の目的関数**である。他は「rebase の総負担
    （誰がどれだけ払うか）」を最小化するのに対し、これは「手を入れずに
    landing できる件数」を最大化する。件数は増えるが、後回しにされた PR の
    rebase 負担はむしろ増えうる。UI ではこの違いを明示すること。

    各ステップで、残りのうち累積ツリーへ clean に載るものを実際に試す。
    O(n²) 回の merge-tree だが 1 回が数 ms なので、48 件でも数秒で終わる。
    """
    preds = predecessors or {}
    remaining = [p for p in pool if p in candidates]
    acc = line_oid
    ordered: list[str] = []
    placed: set[str] = set()
    deferred: list[str] = []

    while remaining:
        ready = [p for p in remaining if not (preds.get(p, set()) - placed)]
        if not ready:
            ready = list(remaining)

        landed = None
        for pr_id in sorted(ready, key=tie_break) if tie_break else sorted(ready):
            cand = candidates[pr_id]
            if cand.has_base_conflict:
                continue
            try:
                r = repo.merge_tree(merge_base=line_oid, ours=acc, theirs=cand.landing_tree)
            except MergeTreeError:
                continue
            if r.clean:
                # `simulate` と同じ理由で tree OID のまま持ち回す
                acc = r.tree_oid
                landed = pr_id
                break

        if landed is None:
            # どれも clean に載らない。残りは後ろに送るが、**スタックの
            # 先行制約は保ったまま**並べる。単純な名前順にすると
            # フォーク側の PR id が祖先の upstream 側 PR id より
            # 前に来てしまうことがある（辞書順は所有者名で決まるため）。
            deferred.extend(_topological(remaining, preds))
            break

        ordered.append(landed)
        placed.add(landed)
        remaining.remove(landed)

    return ordered + deferred


def best_landing_order(
    repo: "Repo",
    line_oid: str,
    candidates: dict[str, Candidate],
    pool: list[str],
    predecessors: dict[str, set[str]] | None = None,
    *,
    restarts: int = 12,
    seed: int = 0,
) -> tuple[list[str], int, Simulation]:
    """landing 件数が最大の順序を探す。

    返り値は (順序, 実測 landing 件数, その順序の Simulation)。3 番目を
    返すのは、呼び出し側が同じ順序をもう一度流さずに済むようにするため。

    単純な貪欲は近視眼的で、実運用のデータでは推奨順と同じ件数に留まり、
    ランダム探索が見つけた最良に届かなかった。走査順を変えた
    リスタートを重ねて最良を採る。

    **ここが検証フェーズの支配項**である（`greedy_max_landing` 1 本が
    O(n²) 回の merge-tree で、それを 1+restarts 本走らせる）。各本は
    互いに完全に独立なので並列に流すが、**畳み込みは投入順で行う** ——
    完了順に畳むと同点のときに選ばれる順序が実行ごとに変わる。
    """
    import random

    # 乱数は先に使い切る。並列実行でも消費順が変わらないようにするため。
    rng = random.Random(seed)
    scan = [p for p in pool if p in candidates]
    ranks: list[dict[str, int] | None] = [None]  # None = 素の貪欲（基準）
    for _ in range(restarts):
        shuffled = scan[:]
        rng.shuffle(shuffled)
        ranks.append({p: i for i, p in enumerate(shuffled)})

    def attempt(rank: dict[str, int] | None) -> tuple[list[str], Simulation]:
        order = greedy_max_landing(
            repo,
            line_oid,
            candidates,
            pool,
            predecessors,
            tie_break=None if rank is None else (lambda p: rank.get(p, 0)),
        )
        return order, simulate(repo, line_oid, order, candidates)

    results = parallel.imap(attempt, ranks)
    best_order, best_sim = next(results)
    for order, sim in results:
        if sim.merged > best_sim.merged:
            best_order, best_sim = order, sim

    return best_order, best_sim.merged, best_sim


def probe_order_sensitivity(
    repo: "Repo",
    line_oid: str,
    candidates: dict[str, Candidate],
    base_order: list[str],
    *,
    trials: int = 30,
    seed: int = 0,
    baseline: Simulation | None = None,
) -> dict:
    """順序を変えるとマージできる件数が変わるのかを実測で確かめる。

    交通整理の合意形成では、ここを曖昧にできない。**もし件数が順序に
    依存しないなら、「どの順で流すか」の議論は「誰が rebase するか」の
    議論でしかない** —— それは正当な議論だが、「この順序なら多く流せる」
    という主張は誤りになる。推測ではなく実際に流して確かめる。

    シミュレーション 1 本は O(n) 回の merge-tree で数十 ms なので、
    数十通り試しても問題にならない。

    :param baseline: `base_order` の Simulation が既にあるなら渡す。
        呼び出し側は同じ順序をプリセットの検証で流しているので、
        既算のものを渡せば 1 本まるごと省ける。
    """
    import random

    # 乱数は先に使い切る（並列実行でも消費順を固定する）。
    rng = random.Random(seed)
    pool = [p for p in base_order if p in candidates]
    trial_orders: list[list[str]] = []
    for _ in range(trials):
        shuffled = pool[:]
        rng.shuffle(shuffled)
        trial_orders.append(shuffled)

    if baseline is None:
        baseline = simulate(repo, line_oid, base_order, candidates)
    best = worst = baseline.merged
    best_order = list(base_order)

    sims = parallel.imap(
        lambda o: simulate(repo, line_oid, o, candidates), trial_orders
    )
    # 投入順に畳む（完了順にすると同点のときの best_order が揺れる）
    for shuffled, s in zip(trial_orders, sims):
        if s.merged > best:
            best, best_order = s.merged, shuffled
        worst = min(worst, s.merged)

    return {
        "recommended_merged": baseline.merged,
        "best_observed": best,
        "worst_observed": worst,
        "trials": trials,
        # 件数が動かないなら、順序が変えるのは「誰が払うか」だけ
        "order_invariant": best == worst == baseline.merged,
        "best_order": best_order if best > baseline.merged else None,
    }


def to_dict(sim: Simulation) -> dict:
    return {
        "merged": sim.merged,
        "conflicted": sim.conflicted,
        "steps": [
            {
                "index": s.index,
                "pr": s.pr_id,
                "result": s.result,
                **({"detail": s.detail} if s.detail else {}),
                **(
                    {
                        "conflict_files": [
                            {"path": c.path, "stages": sorted(c.stages), "types": list(c.types)}
                            for c in s.conflict_files
                        ]
                    }
                    if s.conflict_files
                    else {}
                ),
            }
            for s in sim.steps
        ],
    }

"""出力 JSON の組み立て。ビューアとの契約。

規約:

* `pairs` に載っていないペアは **L0（無干渉）**。61 PR でも大半が L0 に
  なるため、全部書くと肥大する。`pairs_evaluated` があるので、ビューアは
  「クリーン」と「未計算」を区別できる。
* 干渉ペアに `blocks` / `blocked_by` のような **方向性を持つ名前は使わない**。
  計算しているのは統合ラインに対する *対称的な* 同時マージ可能性であって、
  どちらを先にすべきかの非対称性ではない。誤解を招く命名を避ける。
"""

from __future__ import annotations

import datetime as _dt
from typing import TYPE_CHECKING

from .dag import StackGraph
from .model import Candidate, ConflictFile, PairResult, Skip


def conflict_file_dict(c: ConflictFile) -> dict:
    """`ConflictFile` の JSON 表現。ビューアとの契約なので 1 箇所に持つ。

    ベース衝突（`pull_requests[].base_conflict_files`）と逐次マージの
    衝突（`orders[].presets[].simulation.steps[].conflict_files`）で
    同じ形を出す。別々に書くと片方だけフィールドが増える。
    """
    return {"path": c.path, "stages": sorted(c.stages), "types": list(c.types)}

if TYPE_CHECKING:
    from .gitops import Repo

SCHEMA_VERSION = 1


def _pair_dict(p: PairResult) -> dict:
    d: dict = {"a": p.a, "b": p.b, "relation": p.relation.value}
    if p.level is not None:
        d["level"] = int(p.level)
    if p.is_comment_only:
        d["comment_only"] = True
    if p.files:
        # ファイルごとに等級を持つ。ペアの level はその最大値でしかない。
        d["files"] = [
            {
                "path": f.path,
                "level": int(f.level),
                **({"stages": sorted(f.stages)} if f.stages else {}),
                **({"types": list(f.types)} if f.types else {}),
                **({"structural": True} if f.is_structural else {}),
                **({"comment_only": True} if f.comment_only else {}),
                **(
                    {
                        "hunks": [
                            {
                                "line": h.start_line,
                                "a": list(h.ours),
                                "b": list(h.theirs),
                                **({"a_truncated": True} if h.ours_truncated else {}),
                                **({"b_truncated": True} if h.theirs_truncated else {}),
                                # チャンク単位の判定。ファイルに丸めると
                                # 「1 箇所だけ実コード」が見えなくなる。
                                **({"comment_only": True} if h.comment_only else {}),
                            }
                            for h in f.hunks
                        ]
                    }
                    if f.hunks
                    else {}
                ),
                **(
                    {
                        "warnings": [
                            {
                                "kind": w.kind.value,
                                "detail": w.detail,
                                **({"symbols": list(w.symbols)} if w.symbols else {}),
                            }
                            for w in f.warnings
                        ]
                    }
                    if f.warnings
                    else {}
                ),
            }
            for f in p.files
        ]
    if p.overlap_files:
        d["overlap_files"] = sorted(p.overlap_files)
    if p.warnings:
        d["warnings"] = [
            {
                "kind": w.kind.value,
                "path": w.path,
                "detail": w.detail,
                **({"symbols": list(w.symbols)} if w.symbols else {}),
                **({"ranges": [list(r) for r in w.ranges]} if w.ranges else {}),
            }
            for w in p.warnings
        ]
    return d


def build(
    *,
    target: str,
    forks: list[str],
    git_version: str,
    graph: StackGraph,
    line_names: list[str],
    line_oids: dict[str, str],
    candidates: dict[str, list[Candidate]],
    pairs: dict[str, list[PairResult]],
    orders: dict,
    skipped: list[Skip],
    file_changes: dict,
    duration: float,
    repo: "Repo",
) -> dict:
    cand_by_id: dict[str, Candidate] = {
        c.id: c for cands in candidates.values() for c in cands
    }

    # --- 統合ラインの関係 -------------------------------------------
    lines_out = []
    for name in line_names:
        entry = {
            "id": name,
            "repo": target,
            "branch": name,
            "head_oid": line_oids[name],
            "pr_count": len(candidates.get(name, [])),
        }
        others = [o for o in line_names if o != name]
        if others:
            other = others[0]
            mb = repo.merge_base(line_oids[name], line_oids[other])
            if mb:
                ahead, behind = repo.count_divergence(line_oids[name], line_oids[other])
                entry["diverged_from"] = {
                    "line": other,
                    "merge_base": mb,
                    "ahead": ahead,
                    "behind": behind,
                    "is_descendant": repo.is_ancestor(line_oids[other], line_oids[name]),
                }
        lines_out.append(entry)

    # --- PR ----------------------------------------------------------
    prs_out = []
    warnings_out = [
        {
            "kind": w.kind,
            "severity": w.severity,
            "subjects": list(w.subjects),
            "detail": w.detail,
        }
        for w in graph.warnings
    ]

    duplicate_partners: dict[str, list[str]] = {}
    for w in graph.warnings:
        if w.kind == "duplicate_pr_head":
            for s in w.subjects:
                duplicate_partners[s] = [x for x in w.subjects if x != s]

    for pr_id in sorted(graph.prs):
        pr = graph.prs[pr_id]
        res = graph.resolutions[pr_id]
        cand = cand_by_id.get(pr_id)
        entry = {
            "id": pr_id,
            "repo": pr.repo,
            "number": pr.number,
            "title": pr.title,
            "url": pr.url,
            "author": pr.author,
            "author_avatar_url": pr.author_avatar_url,
            "review_notes": [
                {
                    "author": n.author,
                    "state": n.state,
                    "body": n.body,
                    **({"path": n.path} if n.path else {}),
                    **({"line": n.line} if n.line is not None else {}),
                    **({"url": n.url} if n.url else {}),
                    **({"outdated": True} if n.outdated else {}),
                }
                for n in pr.review_notes
            ],
            "kind": "external_pr" if pr.repo != target else "pr",
            "is_cross_repository": pr.is_cross_repository,
            "head": {
                "repo": pr.head_repo,
                "branch": pr.head_branch,
                "oid": pr.head_oid,
            },
            "base": {"repo": pr.base_repo, "branch": pr.base_branch},
            "line": res.line,
            "line_resolution": res.resolution,
            "stack": {"depth": res.depth, "ancestors": list(res.ancestors)},
            "blocks": sorted(graph.descendants_of(pr_id)),
            "is_draft": pr.is_draft,
            "review_decision": pr.review_decision,
            "github_mergeable": pr.github_mergeable,
            "additions": pr.additions,
            "deletions": pr.deletions,
            "changed_files_count": pr.changed_files_count,
            "created_at": pr.created_at,
            "updated_at": pr.updated_at,
        }
        if pr_id in duplicate_partners:
            entry["duplicate_of"] = duplicate_partners[pr_id]
        if cand is not None:
            entry["status"] = "base_conflict" if cand.has_base_conflict else "ok"
            entry["base_conflict"] = cand.has_base_conflict
            entry["landing_tree"] = cand.landing_tree
            entry["changed_files"] = sorted(cand.changed_files)
            if cand.base_conflicts:
                entry["base_conflict_files"] = [
                    conflict_file_dict(c) for c in cand.base_conflicts
                ]
            # GitHub の判定とローカル実測の食い違いは、それ自体が
            # 「GitHub の状態がおかしい」という当初の問題の証拠になる
            gh_conflicting = pr.github_mergeable == "CONFLICTING"
            if gh_conflicting != cand.has_base_conflict:
                entry["github_mergeable_mismatch"] = True
                warnings_out.append(
                    {
                        "kind": "github_mergeable_mismatch",
                        "severity": "info",
                        "subjects": [pr_id],
                        "detail": (
                            f"GitHub は {pr.github_mergeable} と報告しているが、"
                            f"ローカル解析では base 衝突 = {cand.has_base_conflict}"
                        ),
                    }
                )
        else:
            entry["status"] = "excluded"
        prs_out.append(entry)

    for s in skipped:
        warnings_out.append(
            {
                "kind": "pr_excluded",
                "severity": "warn",
                "subjects": [s.pr_id],
                "detail": s.reason,
            }
        )

    # --- 干渉 ---------------------------------------------------------
    interference_out = {}
    total_conflicts = 0
    for name in line_names:
        ps = pairs.get(name, [])
        n = len(candidates.get(name, []))
        counts: dict[str, int] = {}
        for p in ps:
            key = f"L{int(p.level)}" if p.level is not None else p.relation.value
            counts[key] = counts.get(key, 0) + 1
        evaluated = n * (n - 1) // 2
        counts["L0"] = evaluated - len(ps)
        total_conflicts += sum(1 for p in ps if p.is_conflict)
        interference_out[name] = {
            "pairs_evaluated": evaluated,
            "level_counts": counts,
            "pairs": [_pair_dict(p) for p in ps],
        }

    # --- 順序 ---------------------------------------------------------
    orders_out = {}
    for name, plan in orders.items():
        orders_out[name] = {
            "clusters": [
                {"id": c.id, "members": c.members, "internal_pairs": c.internal_pairs}
                for c in plan.clusters
            ],
            "independent": plan.independent,
            "undetermined": plan.undetermined,
            "order_sensitivity": plan.order_sensitivity,
            "presets": plan.presets,
            "metrics": {
                k: {
                    "regret": round(m.regret, 3),
                    "blocks": m.blocks,
                    "blast_radius": m.blast_radius,
                    "rebase_load": round(m.rebase_load, 3),
                }
                for k, m in plan.metrics.items()
            },
        }

    # --- 行動可能なヘッドライン -----------------------------------------
    # 「ベースと衝突している N 件を rebase すれば M ペアの解析が可能になる」
    # は、このツールが出せる最も具体的な指示なので、埋もれさせずに前面へ出す。
    actions = []

    # 指定し忘れた統合ラインは、黙って除外すると「衝突 0 件」という
    # 誤った安心を与える。何件がどこにぶら下がっているかを前面に出す。
    unlisted: dict[str, list[str]] = {}
    for s in skipped:
        if s.kind != "unlisted_line":
            continue
        unlisted.setdefault(s.branch, []).append(s.pr_id)
    for branch, ids in sorted(unlisted.items(), key=lambda kv: -len(kv[1])):
        actions.append(
            {
                "kind": "unlisted_integration_line",
                "line": line_names[0] if line_names else "",
                "branch": branch,
                "pr_count": len(ids),
                "prs": sorted(ids),
                "message": (
                    f"{len(ids)} 件の PR がブランチ '{branch}' に向いていますが、"
                    f"解析対象の統合ラインに含まれていないため除外されています。"
                    f"統合先として扱うなら lines に '{branch}' を追加してください。"
                ),
            }
        )

    for name in line_names:
        blocked = [c.id for c in candidates.get(name, []) if c.has_base_conflict]
        degraded = interference_out[name]["level_counts"].get("degraded", 0)
        if blocked:
            actions.append(
                {
                    "kind": "rebase_to_unblock_analysis",
                    "line": name,
                    "pr_count": len(blocked),
                    "prs": sorted(blocked),
                    "unlocks_pairs": degraded,
                    "message": (
                        f"{name}: ベースと衝突している {len(blocked)} 件を rebase すると、"
                        f"解析できない {degraded} ペアが解析可能になる"
                    ),
                }
            )
        plan = orders.get(name)
        sens = plan.order_sensitivity if plan else None
        if sens and sens.get("order_invariant"):
            actions.append(
                {
                    "kind": "order_does_not_change_throughput",
                    "line": name,
                    "merged": sens["recommended_merged"],
                    "trials": sens["trials"],
                    "message": (
                        f"{name}: どの順序でも clean にマージできるのは "
                        f"{sens['recommended_merged']} 件で変わらない"
                        f"（{sens['trials']} 通り試行）。順序が決めるのは"
                        "「誰が rebase するか」だけ。"
                    ),
                }
            )

    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": _dt.datetime.now(_dt.timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z"),
        "source": {
            "repo": target,
            "forks_scanned": forks,
            "git_version": git_version,
        },
        "integration_lines": lines_out,
        "actions": actions,
        "pull_requests": prs_out,
        "interference": interference_out,
        "file_changes": file_changes,
        "orders": orders_out,
        "warnings": warnings_out,
        "stats": {
            "prs_total": len(graph.prs),
            "prs_analyzed": len(cand_by_id),
            "prs_skipped": len(skipped),
            "conflict_pairs": total_conflicts,
            "base_conflicts": sum(1 for c in cand_by_id.values() if c.has_base_conflict),
            "duration_sec": round(duration, 2),
        },
    }

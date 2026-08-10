"""解析のエントリポイント。

    python -m analyzer.analyze --repo OWNER/NAME --lines main --out out.json

フェーズの順序を制御するだけの薄い層に保ち、判断は各モジュールに置く。
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
import time
from pathlib import Path

from . import dag, github, interference, order, report, simulate
from .gitops import GitVersionError, MergeTreeError, Repo, assert_git_version, clone
from .model import Candidate, PullRequest


def _remote_namespace(repo: str) -> str:
    return repo.split("/", 1)[0].lower().replace(".", "-")


def prepare_repo(
    target: str, forks: list[str], workdir: Path, *, verbose: bool = True
) -> tuple[Repo, dict[str, str]]:
    """対象とフォークを 1 つのリポジトリに集める。

    完全 clone すること（`--filter=blob:none` は merge-tree が blob を
    オンデマンド取得して桁違いに遅くなる）。checkout は不要 ——
    merge-tree は index も worktree も触らない。
    """
    if verbose:
        print(f"  clone {target} ...", file=sys.stderr)
    repo = clone(f"https://github.com/{target}.git", workdir / "repo")
    namespaces = {target: _remote_namespace(target)}
    repo.run(
        "fetch",
        "--quiet",
        "origin",
        f"+refs/pull/*/head:refs/remotes/{namespaces[target]}-pr/*",
        "+refs/heads/*:refs/remotes/origin/*",
    )
    for fork in forks:
        ns = _remote_namespace(fork)
        namespaces[fork] = ns
        if verbose:
            print(f"  fetch fork {fork} ...", file=sys.stderr)
        repo.run("remote", "add", ns, f"https://github.com/{fork}.git")
        repo.run(
            "fetch",
            "--quiet",
            ns,
            f"+refs/pull/*/head:refs/remotes/{ns}-pr/*",
            f"+refs/heads/*:refs/remotes/{ns}-br/*",
        )
    return repo, namespaces


def run(
    target: str,
    line_names: list[str],
    *,
    include_forks: bool = True,
    workdir: Path | None = None,
    verbose: bool = True,
) -> dict:
    started = time.time()
    git_ver = assert_git_version()

    if verbose:
        print(f"PR を収集中 ({target}) ...", file=sys.stderr)
    prs, forks = github.collect(target, include_forks=include_forks)
    if verbose:
        print(f"  PR {len(prs)} 件, フォーク {forks}", file=sys.stderr)

    tmp = Path(tempfile.mkdtemp(prefix="pr-conflict-")) if workdir is None else Path(workdir)
    cleanup = workdir is None
    try:
        repo, _ = prepare_repo(target, forks, tmp, verbose=verbose)

        lines = {dag.node_key(target, name): name for name in line_names}
        line_oids = {name: repo.rev_parse(f"origin/{name}") for name in line_names}

        # そのブランチを base にしているオープン PR の数。
        # 多くの PR が直接ぶら下がっているブランチは「スタックの親」ではなく
        # **統合ラインそのもの**なので、勝手に他のラインへ寄せてはいけない。
        base_counts: dict[str, int] = {}
        for p in prs:
            key = dag.node_key(p.base_repo, p.base_branch)
            base_counts[key] = base_counts.get(key, 0) + 1

        #: これ以上の PR が直接ぶら下がっていたら、独立した統合ラインとみなす
        LINE_LIKE_THRESHOLD = 3
        unlisted_lines: dict[str, int] = {}

        def infer_line(node: str) -> str | None:
            """親 PR 不在のルートブランチが、どのラインに属するか推定する。

            推定してよいのは「スタックの親だった（が PR が閉じられた）」
            ような枝だけ。多数の PR がぶら下がっているブランチは、
            指定し忘れた統合ラインである可能性が高く、そこへ寄せると
            分岐したブランチの PR を無関係なラインへ着地させて
            大量の偽のベース衝突を生む。**推定せず除外して警告する。**
            """
            if base_counts.get(node, 0) >= LINE_LIKE_THRESHOLD:
                unlisted_lines[node] = base_counts[node]
                return None

            branch = node.split(":", 1)[1]
            ref = f"origin/{branch}"
            if not repo.exists(ref):
                return None
            best, best_dist = None, None
            for name, oid in line_oids.items():
                mb = repo.merge_base(oid, ref)
                if mb is None:
                    continue
                # ラインから見て何コミット離れているかで近さを測る
                ahead, _ = repo.count_divergence(oid, mb)
                if best_dist is None or ahead < best_dist:
                    best, best_dist = name, ahead
            return best

        graph = dag.build(prs, lines, infer_line=infer_line)

        # --- 着地tree の計算 -------------------------------------------
        if verbose:
            print("着地tree を計算中 ...", file=sys.stderr)
        by_line: dict[str, list[Candidate]] = {name: [] for name in line_names}
        skipped: list[tuple[str, str]] = []

        for node, count in sorted(unlisted_lines.items()):
            branch = node.split(":", 1)[1]
            print(
                f"  注意: ブランチ '{branch}' に {count} 件の PR が直接ぶら下がっています。"
                f"統合ラインとして扱うなら --lines に追加してください"
                f"（現状これらの PR は解析から除外されます）。",
                file=sys.stderr,
            )

        for pr_id in sorted(graph.prs):
            pr = graph.prs[pr_id]
            res = graph.resolutions[pr_id]
            if res.line is None or res.line not in by_line:
                root_branch = res.root_node.split(":", 1)[1]
                if res.root_node in unlisted_lines:
                    reason = (
                        f"'{root_branch}' は指定された統合ラインに含まれていない"
                        f"（{unlisted_lines[res.root_node]} 件の PR がぶら下がる別の統合先）"
                    )
                else:
                    reason = f"統合ラインを解決できない ({res.resolution})"
                skipped.append((pr_id, reason))
                continue
            if not repo.exists(pr.head_oid):
                skipped.append((pr_id, "head コミットがローカルに存在しない"))
                continue
            try:
                cand = interference.build_candidate(
                    repo,
                    line_oids[res.line],
                    pr_id,
                    pr.head_oid,
                    ancestors=frozenset(res.ancestors),
                )
            except MergeTreeError as exc:
                skipped.append((pr_id, f"merge-tree エラー: {exc}"))
                continue
            by_line[res.line].append(cand)

        # --- ペアワイズ -------------------------------------------------
        results = {}
        for name, cands in by_line.items():
            if verbose:
                n = len(cands)
                print(f"  {name}: {n} 件 -> {n*(n-1)//2} ペア", file=sys.stderr)
            results[name] = interference.analyze_line(repo, line_oids[name], cands)

        # --- 順序推奨 ---------------------------------------------------
        if verbose:
            print("マージ順を計算中 ...", file=sys.stderr)
        orders = {
            name: order.plan_line(name, by_line[name], results[name], graph)
            for name in line_names
        }

        # --- 逐次マージシミュレーションによる検証 -----------------------
        # 代理モデル（ペアワイズ）は累積ツリー効果を捉えないので、
        # 推奨順を実際に流して裏を取る。全順序は試せない（1 本あたり
        # O(n) 回の merge-tree が要る）ので、プリセットの分だけに絞る。
        if verbose:
            print("推奨順を逐次マージで検証中 ...", file=sys.stderr)
        for name in line_names:
            cand_map = {c.id: c for c in by_line[name]}
            predicted = {
                pid
                for p in results[name]
                if p.is_conflict
                for pid in (p.a, p.b)
            }
            # 「landing 件数の最大化」は他プリセットと目的関数が違うので、
            # 実際に流しながら貪欲に構成する（代理モデルでは作れない）。
            preds = order._predecessors(sorted(cand_map), graph)
            best_order, _best_merged = simulate.best_landing_order(
                repo, line_oids[name], cand_map, sorted(cand_map), preds
            )
            orders[name].presets["max-landing"] = {
                "order": best_order,
                "optimal": False,
                "method": "greedy_simulation_with_restarts",
                "objective_note": (
                    "clean に landing できる件数を最大化する。"
                    "他プリセットが最小化する rebase 総負担はむしろ増えうる。"
                ),
            }

            for preset_name, preset in orders[name].presets.items():
                sim = simulate.simulate(
                    repo, line_oids[name], preset["order"], cand_map
                )
                preset["simulation"] = simulate.to_dict(sim)
                preset["predicted_vs_actual"] = simulate.compare_with_matrix(sim, predicted)

            # 「順序を変えれば多く流せる」のかを実測で確かめる。
            # 実データでは順序に依存しなかった（＝順序が決めるのは
            # 誰が rebase するかだけ）ので、それを検証済みの主張として残す。
            orders[name].order_sensitivity = simulate.probe_order_sensitivity(
                repo,
                line_oids[name],
                cand_map,
                orders[name].presets["balanced"]["order"],
            )

        return report.build(
            target=target,
            forks=forks,
            git_version=".".join(map(str, git_ver)),
            graph=graph,
            line_names=line_names,
            line_oids=line_oids,
            candidates=by_line,
            pairs=results,
            orders=orders,
            skipped=skipped,
            duration=time.time() - started,
            repo=repo,
        )
    finally:
        if cleanup:
            shutil.rmtree(tmp, ignore_errors=True)


def slug(repo: str) -> str:
    """リポジトリ名を出力ファイル名に使える形にする。"""
    return repo.replace("/", "-").lower()


def load_index(outdir: Path) -> dict:
    """既存の索引を読む。無ければ空の索引を返す。"""
    path = outdir / "index.json"
    if not path.exists():
        return {"schema_version": report.SCHEMA_VERSION, "analyses": []}
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError:
        return {"schema_version": report.SCHEMA_VERSION, "analyses": []}


def write_index(outdir: Path, analyses: list[dict]) -> None:
    """索引を書き出す。

    並び順はリポジトリ名の辞書順に固定する。解析対象のあいだに
    優劣や主従があるように見せないため（また、実行順によって
    並びが変わると差分が無駄に大きくなる）。
    """
    analyses = sorted(analyses, key=lambda e: e["repo"].lower())
    (outdir / "index.json").write_text(
        json.dumps(
            {
                "schema_version": report.SCHEMA_VERSION,
                "updated_at": max((e["generated_at"] for e in analyses), default=""),
                "analyses": analyses,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n"
    )


def analyze_and_cache(
    targets: list[dict], outdir: Path, *, verbose: bool = True
) -> int:
    """対象を解析し、結果を索引に**追加または更新**する。

    索引は蓄積型のキャッシュとして扱う。一度解析したリポジトリは
    索引に残り、次からは定期実行で更新されていく。固定の対象リストを
    リポジトリに持たないので、「何のために作られたか」が構成から
    読み取れない。

    1 件でも失敗したら終了コードを立てるが、**残りの解析は続ける**。
    1 つのリポジトリの一時的な失敗で、他の結果まで古いままに
    なるのを避けるため。
    """
    outdir.mkdir(parents=True, exist_ok=True)
    index = load_index(outdir)
    by_repo = {e["repo"]: e for e in index.get("analyses", [])}
    failures = 0

    for t in targets:
        repo = t["repo"]
        lines = t.get("lines") or ["main"]
        if verbose:
            print(f"\n===== {repo} =====", file=sys.stderr)
        try:
            data = run(
                repo,
                lines,
                include_forks=t.get("include_forks", True),
                verbose=verbose,
            )
        except Exception as exc:  # 1 件の失敗で全体を止めない
            failures += 1
            print(f"  失敗: {repo}: {exc}", file=sys.stderr)
            continue

        path = outdir / f"{slug(repo)}.json"
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
        s = data["stats"]
        by_repo[repo] = {
            "repo": repo,
            "file": path.name,
            "generated_at": data["generated_at"],
            "prs_total": s["prs_total"],
            "conflict_pairs": s["conflict_pairs"],
            "base_conflicts": s["base_conflicts"],
            # 定期更新のときに同じ条件で再解析できるよう、指定を残す
            "lines": lines,
            "include_forks": t.get("include_forks", True),
        }
        if verbose:
            print(f"  -> {path} ({s['prs_total']} PR)", file=sys.stderr)

    write_index(outdir, list(by_repo.values()))
    if verbose:
        print(f"\n索引: {outdir/'index.json'}（{len(by_repo)} 件）", file=sys.stderr)
    return 1 if failures else 0


def forget(repo: str, outdir: Path, *, verbose: bool = True) -> int:
    """蓄積から 1 リポジトリを取り除く。

    解析結果を git に置かない運用では、対象を外す手段が別に要る
    （ファイルを消す PR を出す、という操作ができないため）。
    """
    index = load_index(outdir)
    analyses = index.get("analyses", [])
    remaining = [e for e in analyses if e["repo"] != repo]
    if len(remaining) == len(analyses):
        print(f"{repo} は蓄積に含まれていません。", file=sys.stderr)
        return 1
    for e in analyses:
        if e["repo"] == repo:
            (outdir / e["file"]).unlink(missing_ok=True)
    write_index(outdir, remaining)
    if verbose:
        print(f"{repo} を蓄積から削除しました（残り {len(remaining)} 件）", file=sys.stderr)
    return 0


def targets_from_index(outdir: Path) -> list[dict]:
    """索引に載っているリポジトリを、再解析用の対象リストとして返す。"""
    return [
        {
            "repo": e["repo"],
            "lines": e.get("lines") or ["main"],
            "include_forks": e.get("include_forks", True),
        }
        for e in load_index(outdir).get("analyses", [])
    ]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="PR 間の干渉を解析して JSON を出力する")
    ap.add_argument("--repo", help="解析対象のリポジトリ（OWNER/NAME）。単一実行用")
    ap.add_argument(
        "--lines",
        help="統合ラインのブランチ名（カンマ区切り）。分岐したブランチは別々に解析される",
    )
    ap.add_argument(
        "--out",
        type=Path,
        help="出力先を明示する。索引には登録しない（使い捨ての解析用）",
    )
    ap.add_argument(
        "--refresh",
        action="store_true",
        help="索引に載っているリポジトリをすべて再解析する（定期更新用）",
    )
    ap.add_argument(
        "--forget",
        metavar="OWNER/NAME",
        help="指定したリポジトリを蓄積から取り除く（解析はしない）",
    )
    ap.add_argument("--outdir", type=Path, default=Path("docs/data"))
    ap.add_argument("--no-forks", action="store_true")
    ap.add_argument("--workdir", type=Path, default=None, help="作業リポジトリを残す場所")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)
    verbose = not args.quiet

    if args.forget:
        return forget(args.forget, args.outdir, verbose=verbose)

    try:
        assert_git_version()
    except GitVersionError as exc:
        print(f"エラー: {exc}", file=sys.stderr)
        return 2

    if args.refresh:
        targets = targets_from_index(args.outdir)
        if not targets:
            print(
                "索引が空です。まず --repo で 1 件解析してください。", file=sys.stderr
            )
            return 0
        if verbose:
            print(f"索引の {len(targets)} 件を再解析します", file=sys.stderr)
        return analyze_and_cache(targets, args.outdir, verbose=verbose)

    if not args.repo or not args.lines:
        ap.error("--repo と --lines、または --refresh を指定してください")

    lines = [x.strip() for x in args.lines.split(",") if x.strip()]
    target = {
        "repo": args.repo,
        "lines": lines,
        "include_forks": not args.no_forks,
    }

    # --out を明示したときは使い捨て扱いにして索引を汚さない
    if args.out:
        try:
            data = run(
                args.repo, lines, include_forks=not args.no_forks,
                workdir=args.workdir, verbose=verbose,
            )
        except GitVersionError as exc:
            print(f"エラー: {exc}", file=sys.stderr)
            return 2
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
        if verbose:
            s = data["stats"]
            print(
                f"\n出力: {args.out}\n"
                f"  PR {s['prs_total']} 件 / 衝突ペア {s['conflict_pairs']} 件",
                file=sys.stderr,
            )
        return 0

    return analyze_and_cache([target], args.outdir, verbose=verbose)


if __name__ == "__main__":
    raise SystemExit(main())

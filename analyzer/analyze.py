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
from collections import deque
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from . import dag, filechanges, github, interference, order, parallel, report, simulate
from .gitops import GitVersionError, MergeTreeError, Repo, assert_git_version
from .model import Candidate, PullRequest


class _Timer:
    """フェーズごとの所要時間を測り、内訳を stderr に出す。

    どこに時間が行っているかはログから読めないと分からない（合計だけ見ても
    次に何を直すべきかが決まらない）。**内訳は JSON には入れない** ——
    環境で揺れる数字を成果物に混ぜないため。JSON に出るのは合計
    （`stats.duration_sec`）だけで、それは `active` を使う。
    """

    def __init__(self, verbose: bool = True) -> None:
        self.verbose = verbose
        self.last = time.time()
        self.splits: list[tuple[str, float]] = []

    def split(self, label: str) -> None:
        now = time.time()
        self.splits.append((label, now - self.last))
        self.last = now

    def resume(self) -> None:
        """中断していた時計を再開する（待っていた分は計上しない）。

        準備は先読みで走るので、準備が終わってから解析の順番が来るまでの
        待ちが挟まる。これを計上すると「このリポジトリの解析に何秒かかったか」
        が待ち時間で膨らみ、内訳も次のフェーズに待ちが混ざって読めなくなる。
        """
        self.last = time.time()

    @property
    def active(self) -> float:
        """実際に手を動かしていた合計（待ちを含まない）。"""
        return sum(v for _, v in self.splits)

    def report(self, prefix: str = "  内訳: ") -> None:
        if not self.verbose:
            return
        body = " / ".join(f"{k} {v:.1f}s" for k, v in self.splits)
        print(f"{prefix}{body}（合計 {self.active:.1f}s）", file=sys.stderr)


#: フォークから同時に fetch する本数の上限。ネットワークの相手側で決まる値
#: なのでコア数には連動させない（`github._API_SLOTS` と同じ理由）。
_FETCH_JOBS = 4


def _remote_namespace(repo: str) -> str:
    return repo.split("/", 1)[0].lower().replace(".", "-")


def _refspecs(
    origin: str,
    prs: list[PullRequest],
    repo_name: str,
    namespace: str,
    extra_branches: list[str],
    existing_branches: frozenset[str],
) -> list[str]:
    """`repo_name` から取るべき refspec を、必要な分だけ組み立てる。

    ワイルドカードで全件取ってはいけない。`refs/pull/*/head` には閉じた PR も
    全部入っている —— 実測した対象では 3310 件あり、`refs/heads/*` も 790 件
    あったが、必要な open PR は 134 件だった。

    **`refs/pull/<n>/head` は base 側のリポジトリに作られる。** したがって
    その PR を取れるのは `base_repo` の側だけ。head がフォークにある
    cross-repository PR も、base 側（＝対象リポジトリ）から取れば済む。
    ここを `head_repo` で引くと、フォークに存在しない ref を要求しつつ
    必要な head OID を取り逃し、その PR は「head コミットがローカルに
    存在しない」で静かに解析から落ちる。

    ブランチは `existing_branches` に載っているものだけ挙げる。ワイルドカード
    fetch は存在しない ref を黙って飛ばすが、名前を明示した fetch は
    そこで失敗する。1 本のブランチが消えているだけで解析全体が落ちるのは
    以前より悪い挙動なので、事前に実在するものへ絞る。
    """
    dest = "origin" if repo_name == origin else f"{namespace}-br"
    branches = {b for b in extra_branches if b in existing_branches}
    specs = []
    for p in prs:
        if p.base_repo != repo_name:
            continue
        specs.append(f"+refs/pull/{p.number}/head:refs/remotes/{namespace}-pr/{p.number}")
        if p.base_branch in existing_branches:
            branches.add(p.base_branch)
    # 統合ラインと、PR がぶら下がっているブランチ。`branch_ref()` がこの
    # 名前で引くので、宛先の付け方は変えられない。
    specs.extend(f"+refs/heads/{b}:refs/remotes/{dest}/{b}" for b in sorted(branches))
    return specs


def _remote_branches(repo: Repo, url: str) -> frozenset[str]:
    """リモートに実在するブランチ名。"""
    out = repo.run("ls-remote", "--heads", url)
    return frozenset(
        line.split("refs/heads/", 1)[1]
        for line in out.splitlines()
        if "refs/heads/" in line
    )


def prepare_repo(
    target: str,
    forks: list[str],
    workdir: Path,
    *,
    prs: list[PullRequest],
    line_names: list[str],
    verbose: bool = True,
) -> tuple[Repo, dict[str, str]]:
    """対象とフォークを 1 つのリポジトリに集める。

    オブジェクトは完全に取ること（`--filter=blob:none` は merge-tree が blob を
    オンデマンド取得して桁違いに遅くなる）。checkout は不要 ——
    merge-tree は index も worktree も触らない。

    `git clone` ではなく `git init` + `git fetch <url>` にしている。理由は 2 つ:

    - 取る ref を必要な分だけに絞れる（clone は全ブランチを持ってくる）
    - remote を登録しないので、フォークごとの fetch を並列に流せる
      （`.git/config` の書き換えが競合しない）

    HEAD は未生成のまま残るが、`Repo` のどのメソッドも HEAD を読まない
    （すべて明示 ref か `cat-file`）ので支障はない。
    """
    path = workdir / "repo"
    # `git init` は既存のリポジトリでも成功してしまう（`git clone` は失敗した）。
    # 前回の実行が残した `<ns>-br/*` が混ざると、`branch_ref()` は ref の
    # 存在しか見ないので古いブランチ先端を今回のものとして掴み、
    # `infer_line()` が誤ったラインを推定しうる。使い回しは明示的に断る。
    if (path / ".git").exists() or (path / "HEAD").exists():
        raise RuntimeError(
            f"{path} には既に作業リポジトリがあります。"
            "--workdir は実行ごとに空のディレクトリを指してください"
            "（前回の ref が残ると解析結果が混ざります）。"
        )
    path.mkdir(parents=True, exist_ok=True)
    repo = Repo(path)
    repo.run("init", "--quiet", "--initial-branch=main", ".")

    namespaces = {target: _remote_namespace(target)}
    for fork in forks:
        namespaces[fork] = _remote_namespace(fork)

    def fetch(name: str) -> None:
        url = f"https://github.com/{name}.git"
        specs = _refspecs(
            target,
            prs,
            name,
            namespaces[name],
            line_names if name == target else [],
            _remote_branches(repo, url),
        )
        if not specs:
            return
        if verbose:
            label = "対象" if name == target else "フォーク"
            print(f"  fetch {label} {name} ({len(specs)} ref) ...", file=sys.stderr)
        # --no-write-fetch-head が必須。FETCH_HEAD は remote 名に依らず
        # 共有なので、これが無いと並列 fetch がロックで落ちる。
        repo.run("fetch", "--quiet", "--no-tags", "--no-write-fetch-head", url, *specs)

    # 対象リポジトリを先に単独で取る。統合ラインが揃っていないと後段が
    # 何も判断できないので、フォークに手を伸ばす前にここで確かめる。
    fetch(target)
    missing = [n for n in line_names if not repo.exists(f"origin/{n}")]
    if missing:
        raise RuntimeError(
            f"統合ライン {missing} が {target} に見つかりません。"
            "--lines の指定を確認してください。"
        )
    # フォークの fetch は独立だが、**並列度はコア数に連動させない**。
    # 相手は同じ github.com で、しかも複数リポジトリの準備が先読みで同時に
    # 走るので、コア数のままだと「コア数 × 先読み数」本の接続が並ぶ。
    # 1 本でも一時的に失敗するとこのリポジトリの解析ごと落ちる（git 側には
    # `_gh` のような再試行が無い）ので、本数を抑える方が速さより効く。
    list(parallel.imap(fetch, forks, cap=_FETCH_JOBS))
    return repo, namespaces


@dataclass
class Prepared:
    """解析を始められる状態。ネットワーク律速な準備が済んだところ。

    `prepare` と `analyze_prepared` を分けているのは、**準備（ネットワーク律速）を
    別のリポジトリの解析（CPU 律速）と重ねられるようにするため**。実測では
    大きいリポジトリの clone が全体の 3 割近くを占めていて、その待ちは
    他のリポジトリの計算で埋められる。
    """

    target: str
    line_names: list[str]
    prs: list[PullRequest]
    forks: list[str]
    repo: Repo
    namespaces: dict[str, str]
    git_version: tuple[int, ...]
    tmp: Path
    cleanup: bool
    timer: _Timer

    def discard(self) -> None:
        if self.cleanup:
            shutil.rmtree(self.tmp, ignore_errors=True)


def prepare(
    target: str,
    line_names: list[str],
    *,
    include_forks: bool = True,
    workdir: Path | None = None,
    verbose: bool = True,
) -> Prepared:
    """PR のメタデータと、必要な git オブジェクトを揃える。"""
    timer = _Timer(verbose)
    git_ver = assert_git_version()

    if verbose:
        print(f"PR を収集中 ({target}) ... [並列度 {parallel.jobs()}]", file=sys.stderr)
    prs, forks = github.collect(target, include_forks=include_forks)
    if verbose:
        # 準備は複数リポジトリ分が同時に走るので、どの対象の行なのかを明示する
        print(f"  {target}: PR {len(prs)} 件, フォーク {forks}", file=sys.stderr)
    timer.split("PR収集")

    tmp = Path(tempfile.mkdtemp(prefix="pr-conflict-")) if workdir is None else Path(workdir)
    prepared = Prepared(
        target=target,
        line_names=line_names,
        prs=prs,
        forks=forks,
        repo=Repo(tmp / "repo"),  # prepare_repo が作り直す
        namespaces={},
        git_version=git_ver,
        tmp=tmp,
        cleanup=workdir is None,
        timer=timer,
    )
    try:
        prepared.repo, prepared.namespaces = prepare_repo(
            target, forks, tmp, prs=prs, line_names=line_names, verbose=verbose
        )
    except BaseException:
        prepared.discard()
        raise
    timer.split("clone/fetch")
    return prepared


def run(
    target: str,
    line_names: list[str],
    *,
    include_forks: bool = True,
    workdir: Path | None = None,
    verbose: bool = True,
) -> dict:
    return analyze_prepared(
        prepare(
            target,
            line_names,
            include_forks=include_forks,
            workdir=workdir,
            verbose=verbose,
        ),
        verbose=verbose,
    )


def analyze_prepared(prepared: Prepared, *, verbose: bool = True) -> dict:
    """揃った git オブジェクトの上で干渉を解析する（CPU 律速）。"""
    if verbose:
        print(f"\n===== {prepared.target} =====", file=sys.stderr)
    # 準備が終わってから順番が来るまでの待ちは、このリポジトリの所要ではない
    prepared.timer.resume()
    target = prepared.target
    line_names = prepared.line_names
    prs = prepared.prs
    forks = prepared.forks
    repo = prepared.repo
    namespaces = prepared.namespaces
    git_ver = prepared.git_version
    timer = prepared.timer

    try:

        def branch_ref(node: str) -> str | None:
            """ノードの示すブランチに対応するローカル ref を返す。

            フォークのブランチは `origin/` ではなく、そのフォーク用の
            名前空間に取り込んである。ここを間違えると、フォーク内の
            枝に載った PR がすべて「解決できない」になる。
            """
            node_repo, branch = node.split(":", 1)
            if node_repo == target:
                ref = f"origin/{branch}"
            elif node_repo in namespaces:
                ref = f"{namespaces[node_repo]}-br/{branch}"
            else:
                return None
            return ref if repo.exists(ref) else None

        # 統合ラインのノード。対象リポジトリだけでなく、**フォーク側の
        # 同名ブランチも同じラインの別名として登録する**。
        #
        # フォーク内で完結する PR（base がフォークの master など）は、
        # ノードキーにリポジトリが入る都合で `fork:master` という別ノードに
        # なる。別名を張らないと「指定されていない統合ライン」と判定されて
        # まるごと除外される。実際に問うているのは「これを上流へ入れたら
        # どうなるか」なので、上流のラインへ着地させるのが正しい。
        lines = {dag.node_key(target, name): name for name in line_names}
        for fork in forks:
            for name in line_names:
                lines.setdefault(dag.node_key(fork, name), name)
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

            ref = branch_ref(node)
            if ref is None:
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
        timer.split("DAG")

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

        # PR ごとの着地tree は互いに独立なので並列に作る。**結果を積む順序は
        # PR id 順に固定する**（by_line の並びはペアの列挙順を決めるので、
        # 実行ごとに変わると公開 JSON が揺れる）。
        def landing(pr_id: str) -> tuple[str, Candidate | None, str]:
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
                return pr_id, None, reason
            if not repo.exists(pr.head_oid):
                return pr_id, None, "head コミットがローカルに存在しない"
            try:
                cand = interference.build_candidate(
                    repo,
                    line_oids[res.line],
                    pr_id,
                    pr.head_oid,
                    ancestors=frozenset(res.ancestors),
                )
            except MergeTreeError as exc:
                return pr_id, None, f"merge-tree エラー: {exc}"
            return pr_id, cand, ""

        for pr_id, cand, reason in parallel.imap(landing, sorted(graph.prs)):
            if cand is None:
                skipped.append((pr_id, reason))
            else:
                by_line[graph.resolutions[pr_id].line].append(cand)

        timer.split("着地tree")

        # --- ペアワイズ -------------------------------------------------
        results = {}
        for name, cands in by_line.items():
            if verbose:
                n = len(cands)
                print(f"  {name}: {n} 件 -> {n*(n-1)//2} ペア", file=sys.stderr)
            results[name] = interference.analyze_line(repo, line_oids[name], cands)

        # --- ファイル・関数ごとの変更 -----------------------------------
        # 「この場所を、関係する PR がそれぞれどう変えようとしているか」。
        # ペア単位の干渉一覧では 3 件以上が同じ場所を触るときに全体像が
        # 掴めないので、軸を場所側に反転させたものを別に持つ。
        timer.split("ペアワイズ")
        if verbose:
            print("ファイル・関数ごとの変更を収集中 ...", file=sys.stderr)
        file_changes = {}
        for name in line_names:
            conflicted = frozenset(
                f.path for p in results[name] for f in p.conflict_files
            )
            file_changes[name] = filechanges.build(
                repo, line_oids[name], by_line[name], conflicted_paths=conflicted
            )

        timer.split("変更収集")

        # --- 順序推奨 ---------------------------------------------------
        if verbose:
            print("マージ順を計算中 ...", file=sys.stderr)
        orders = {
            name: order.plan_line(name, by_line[name], results[name], graph)
            for name in line_names
        }
        timer.split("順序計算")

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
            best_order, _best_merged, best_sim = simulate.best_landing_order(
                repo, line_oids[name], cand_map, sorted(cand_map), preds
            )
            orders[name].presets["max-landing"] = {
                "order": best_order,
                "optimal": False,
                "method": "greedy_simulation_with_restarts",
                "objective_note": (
                    "手を入れずに（衝突なく）マージできる件数を最大化する。"
                    "他の方針が最小化する rebase の総負担は、むしろ増えることがある。"
                ),
            }

            # max-landing は best_landing_order が既に流した順序そのもの。
            # 同じ順序を 2 回流す意味は無いので結果を使い回す。
            names = list(orders[name].presets)
            sims = dict(
                zip(
                    names,
                    parallel.imap(
                        lambda pn: best_sim
                        if pn == "max-landing"
                        else simulate.simulate(
                            repo, line_oids[name], orders[name].presets[pn]["order"], cand_map
                        ),
                        names,
                    ),
                )
            )
            for preset_name, preset in orders[name].presets.items():
                sim = sims[preset_name]
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
                # 直上で balanced を流したばかり。同じ順序をもう一度流さない。
                baseline=sims["balanced"],
            )

        timer.split("逐次マージ検証")
        timer.report()

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
            file_changes=file_changes,
            duration=timer.active,
            repo=repo,
        )
    finally:
        prepared.discard()


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


#: 先読みしておく準備の数。作業リポジトリは大きい（大きめの対象では単体で
#: 実測 777MB）ので、対象が増えたときに未消費の分でディスクを埋めないよう、
#: 意図的に小さく抑える。
_PREFETCH_DEPTH = 2


def _prepared_stream(targets: list[dict], *, verbose: bool = True):
    """`targets` の順に `(target, Prepared|None, 例外|None)` を返す。

    次のリポジトリの準備を先読みで走らせるが、**返す順序は targets のまま**。
    順序が実行ごとに変わると索引やログの読み方が変わってしまう。

    先読みは「投入する数」で抑える。全件を投入してワーカー側で枠を待つ
    書き方にすると、**投入順に実行されることを前提にしてしまい**、
    そうでない実装（Python 3.14 で実際にそうなる）ではデッドロックする ——
    先の対象が枠を取り切り、消費側は最初の対象を永久に待つ。
    """

    def once(t: dict) -> Prepared:
        return prepare(
            t["repo"],
            t.get("lines") or ["main"],
            include_forks=t.get("include_forks", True),
            verbose=verbose,
        )

    if len(targets) <= 1:
        for t in targets:
            try:
                yield t, once(t), None
            except Exception as exc:
                yield t, None, exc
        return

    pending: deque[tuple[dict, "Future[Prepared]"]] = deque()
    rest = iter(targets)

    with ThreadPoolExecutor(max_workers=_PREFETCH_DEPTH) as ex:

        def fill() -> None:
            while len(pending) < _PREFETCH_DEPTH:
                nxt = next(rest, None)
                if nxt is None:
                    return
                pending.append((nxt, ex.submit(once, nxt)))

        try:
            fill()
            while pending:
                t, fut = pending.popleft()
                try:
                    prepared = fut.result()
                except Exception as exc:
                    yield t, None, exc
                    fill()
                    continue
                # 解析のあいだ、既に投入済みの次の準備が走っている。
                # 補充は解析が終わってから（未消費の作業リポジトリを増やさない）。
                yield t, prepared, None
                fill()
        finally:
            # 途中で抜けたとき（想定外の例外・Ctrl-C・消費側の打ち切り）に、
            # 先読み済みの作業リポジトリを残さない。1 件が数百 MB あるので、
            # 残すと次の実行のディスクを削る —— しかも最も起こりやすい
            # 引き金がディスク不足なので、放っておくと悪循環になる。
            for _, fut in pending:
                if fut.cancel():
                    continue
                try:
                    fut.result().discard()
                except BaseException:
                    pass
            pending.clear()


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

    準備（PR 収集と fetch）は次のリポジトリの分を先読みして走らせる。
    ネットワークの待ちを別のリポジトリの計算で埋めるため。解析そのものは
    逐次に保つ —— 内部でコア数まで並列化しているので、ここを重ねても
    取り合いになるだけ。
    """
    outdir.mkdir(parents=True, exist_ok=True)
    index = load_index(outdir)
    by_repo = {e["repo"]: e for e in index.get("analyses", [])}
    failures = 0

    # 明示的に閉じること。ループを抜けるだけではジェネレータの `finally` が
    # 走らない —— 例外が伝播すると traceback がフレームを掴んだままになり、
    # 後片付けが GC 待ちになる。先読み済みの作業リポジトリは数百 MB あるので、
    # 片付くタイミングを運に任せない。
    def handle(t: dict, prepared: Prepared, lines: list[str]) -> dict:
        """1 件を解析して書き出し、索引の項目を返す。

        書き出しまでを 1 件分の処理に含める。呼び出し側の `except` の外に
        置くと、ディスク不足のような「1 件の失敗」が全体を止めてしまう
        （そして残りのリポジトリの結果まで古いままになる）。
        """
        repo = t["repo"]
        data = analyze_prepared(prepared, verbose=verbose)
        path = outdir / f"{slug(repo)}.json"
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
        s = data["stats"]
        if verbose:
            print(f"  -> {path} ({s['prs_total']} PR)", file=sys.stderr)
        return {
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

    stream = _prepared_stream(targets, verbose=verbose)
    try:
        for t, prepared, exc in stream:
            repo = t["repo"]
            lines = t.get("lines") or ["main"]
            # 既存の設定と違う統合ラインで実行すると、蓄積されている設定は
            # 置き換わる。黙って変わると、誰かの指定が別の実行で失われるので
            # 必ずログに残す。
            prev = by_repo.get(repo)
            if prev and (prev.get("lines") or []) != list(lines):
                print(
                    f"  注意: 統合ラインの指定を {prev.get('lines')} から {list(lines)} へ"
                    f"置き換えます。以前の指定は失われます。",
                    file=sys.stderr,
                )
            try:
                if exc is not None:
                    raise exc
                assert prepared is not None
                by_repo[repo] = handle(t, prepared, lines)
            except Exception as err:  # 1 件の失敗で全体を止めない
                failures += 1
                print(f"  失敗: {repo}: {err}", file=sys.stderr)
    finally:
        stream.close()

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

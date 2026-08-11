"""git のラッパ。

`git merge-tree` の引数構成は **このファイルの中だけ** に存在させる。
commit と tree OID が同じ引数位置に混在する呼び方をしているため、
散らばると壊れやすい。

git のバージョンは起動時にアサートする。2.40 未満では
``merge-tree --write-tree`` が無い（あるいは ``-z`` の情報節が
構造化されていない）ため、**フォールバック検出器は用意しない**。
検出器が 2 つあって食い違うのはバグの温床であり、
「古い git では動かない」と明示して落ちる方が安全である。
"""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from .mergetree import EXIT_CLEAN, EXIT_CONFLICT, MergeTreeError, MergeTreeResult, parse

MIN_GIT_VERSION = (2, 40)

#: 関数・クラス定義に見える文脈だけを「同一関数」の判定に使う。
#: トップレベルの変更では context に import 行や辞書キーが入るため、
#: それらを同一視すると偽陽性になる。
_DEF = re.compile(r"^\s*(?:async\s+)?(?:def|class)\s+(\w+)")


def function_name_of(context: str) -> str | None:
    """git の hunk ヘッダが付ける「囲っている文脈」から関数・クラス名を取る。

    hunk を表す型が `gitops.Hunk` と `filechanges.ChangeHunk` の 2 つあり、
    どちらも同じ判定をする。規則を 2 か所に置くと片方だけ直る。
    """
    m = _DEF.match(context)
    return m.group(1) if m else None


class GitVersionError(RuntimeError):
    pass


@dataclass(frozen=True)
class Hunk:
    """変更 hunk。`context` は git が付ける「囲っている関数」の文字列。"""

    start: int
    end: int
    context: str

    @property
    def function_name(self) -> str | None:
        return function_name_of(self.context)


def _git_binary() -> str:
    return os.environ.get("PR_TRAFFIC_CONTROLLER_GIT", "git")


def git_version(binary: str | None = None) -> tuple[int, ...]:
    out = subprocess.run(
        [binary or _git_binary(), "--version"], capture_output=True, text=True, check=True
    ).stdout
    m = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?", out)
    if not m:
        raise GitVersionError(f"git のバージョンを解釈できない: {out!r}")
    return tuple(int(g) for g in m.groups() if g is not None)


def assert_git_version(binary: str | None = None) -> tuple[int, ...]:
    """git が要求バージョンを満たすことを確認する。満たさなければ落とす。"""
    v = git_version(binary)
    if v[:2] < MIN_GIT_VERSION:
        raise GitVersionError(
            f"git {'.'.join(map(str, MIN_GIT_VERSION))} 以上が必要です"
            f"（検出: {'.'.join(map(str, v))}）。\n"
            "  `git merge-tree --write-tree -z` を使うため、これより古い git では"
            "衝突を正しく検出できません。\n"
            "  GitHub Actions の ubuntu-latest（2.43+）で実行するか、\n"
            "  ローカルでは docker（alpine/git）を使ってください:\n"
            "    docker run --rm -v \"$PWD\":/w -w /w alpine/git ..."
        )
    return v


@dataclass
class Repo:
    """解析対象の作業リポジトリ（bare ではないが checkout はしない）。"""

    path: Path
    binary: str = ""

    #: `changed_hunks` の結果。キーは不変な git オブジェクトなので永続に有効。
    #: ペアワイズ解析は同じ (line, landing_tree, path) を候補ごとに n-1 回
    #: 問い合わせるため、これが無いと O(n²) 回の `git diff -U0` が走る。
    #: ロックは要らない —— dict 操作は atomic で、重複計算しても結果は同じ。
    _hunks: dict = field(default_factory=dict, repr=False, compare=False)

    #: `merge_tree` の結果。キーは 3 つの git オブジェクトなので永続に有効。
    #: 検証フェーズが同じマージを何度も問う —— `best_landing_order` は貪欲探索の
    #: 各リスタートで作った順序をそのまま `simulate` に流し直すので、
    #: 着地した接頭辞の累積 tree 列が丸ごと再現される。リスタート数だけ
    #: 重複するうえ、全シミュレーションの 1 歩目は `ours=line` で共通になる。
    #: `_hunks` と同じ理由でロックは要らない（不変 OID の純関数）。
    _merges: dict = field(default_factory=dict, repr=False, compare=False)

    def __post_init__(self) -> None:
        self.path = Path(self.path)
        self.binary = self.binary or _git_binary()

    # -- 低レベル -------------------------------------------------------

    def run(self, *args: str, check: bool = True, env: dict[str, str] | None = None) -> str:
        cp = self._run_raw(*args, text=True, env=env)
        if check and cp.returncode != 0:
            raise RuntimeError(f"git {' '.join(args)}: {cp.stderr}")
        return cp.stdout

    def _run_raw(
        self, *args: str, text: bool = False, env: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess:
        full_env = dict(os.environ)
        # ロケール依存のメッセージを避ける（型フィールドの安定化）
        full_env.update({"LC_ALL": "C", "LANG": "C"})
        if env:
            full_env.update(env)
        return subprocess.run(
            [self.binary, *args],
            cwd=self.path,
            capture_output=True,
            text=text,
            env=full_env,
        )

    def rev_parse(self, ref: str) -> str:
        return self.run("rev-parse", "--verify", f"{ref}^{{}}").strip()

    def exists(self, oid_or_ref: str) -> bool:
        return self._run_raw("cat-file", "-e", f"{oid_or_ref}^{{commit}}").returncode == 0

    def merge_base(self, a: str, b: str) -> str | None:
        cp = self._run_raw("merge-base", a, b, text=True)
        return cp.stdout.strip() if cp.returncode == 0 else None

    def is_ancestor(self, ancestor: str, descendant: str) -> bool:
        return self._run_raw("merge-base", "--is-ancestor", ancestor, descendant).returncode == 0

    def count_divergence(self, a: str, b: str) -> tuple[int, int]:
        """(a にしかない数, b にしかない数)。"""
        out = self.run("rev-list", "--left-right", "--count", f"{a}...{b}").split()
        return int(out[0]), int(out[1])

    # -- merge-tree -----------------------------------------------------

    def merge_tree(self, *, merge_base: str, ours: str, theirs: str) -> MergeTreeResult:
        """3-way マージを実行し、結果の tree と衝突情報を返す。

        `ours` / `theirs` には commit でも tree OID でも渡せる（両方とも
        実データで検証済み）。`merge_base` は **必ず明示する**。省略すると
        git がマージベースを推論し、合成コミットに対して静かに誤った
        結果を返す。

        結果は `_merges` にメモ化する。**呼び出し側は OID を渡すこと** ——
        ブランチ名のような可変の ref をキーにすると、ref が動いたときに
        古い結果を返す。解析中は ref を動かさないので実害は無いが、
        キーが不変であることがメモ化の前提である。
        """
        key = (merge_base, ours, theirs)
        cached = self._merges.get(key)
        if cached is not None:
            return cached
        result = self._merge_tree_uncached(merge_base=merge_base, ours=ours, theirs=theirs)
        self._merges[key] = result
        return result

    def _merge_tree_uncached(
        self, *, merge_base: str, ours: str, theirs: str
    ) -> MergeTreeResult:
        cp = self._run_raw(
            "merge-tree",
            "--write-tree",
            "-z",
            f"--merge-base={merge_base}",
            ours,
            theirs,
        )
        if cp.returncode == EXIT_CLEAN:
            return parse(cp.stdout, clean=True)
        if cp.returncode == EXIT_CONFLICT:
            return parse(cp.stdout, clean=False)
        # ここを「非ゼロ＝衝突」にすると引数エラー(129)や壊れたオブジェクトが
        # 偽の衝突として静かに混入する。必ず例外にする。
        raise MergeTreeError(cp.returncode, cp.stderr.decode("utf-8", "replace"))

    def landing_tree(self, line: str, head: str) -> MergeTreeResult:
        """PR を統合ライン `line` に着地させた結果の tree。

        スタックした PR の head はその祖先 PR のコミットを歴史に含むので、
        これは自動的に「累積ビュー」になる。
        """
        mb = self.merge_base(line, head)
        if mb is None:
            raise MergeTreeError(-1, f"{line} と {head} に共通祖先が無い")
        return self.merge_tree(merge_base=mb, ours=line, theirs=head)

    def pair_merge(self, line: str, tree_a: str, tree_b: str) -> MergeTreeResult:
        """2 つの着地tree を統合ラインをベースとしてマージする。

        「A が先に入ったら B は衝突するか」という問いに対応する。
        `merge-base(A, B)` を使う素朴な方法では別の問いを解いてしまう。
        """
        return self.merge_tree(merge_base=line, ours=tree_a, theirs=tree_b)

    # -- diff -----------------------------------------------------------

    def changed_files(self, base: str, target: str) -> frozenset[str]:
        """変更されたパスの集合。

        **`--no-renames` が必須。** 既定では git が改名を検出し、改名された
        ファイルを新しいパス 1 件だけとして報告する。すると「片方が
        `base.py` を削除し、もう片方が `base.py` を `renamed.py` に改名した」
        ようなペアで集合が重ならず、L0（無干渉）と誤判定して merge-tree の
        呼び出しごと飛ばしてしまう —— 実際には rename/delete という
        最も深刻な部類の構造衝突である。

        改名を分解して旧パス・新パスの両方を出せば、重複判定は保守的側に
        倒れる。merge-tree 側の改名検出は無効化しないので、衝突の種別は
        従来どおり検出できる。
        """
        out = self._run_raw("diff", "--name-only", "--no-renames", "-z", base, target)
        if out.returncode != 0:
            raise RuntimeError(f"git diff failed: {out.stderr!r}")
        names = out.stdout.split(b"\0")
        return frozenset(n.decode("utf-8", "surrogateescape") for n in names if n)

    def show(self, tree: str, path: str) -> str | None:
        """tree の中のファイル内容。存在しなければ None。"""
        cp = self._run_raw("show", f"{tree}:{path}", text=False)
        if cp.returncode != 0:
            return None
        return cp.stdout.decode("utf-8", "replace")

    def changed_hunks(self, base: str, target: str, path: str) -> list[Hunk]:
        """変更 hunk を、`base` 座標系の行範囲と**囲っている関数名**とともに返す。

        `git diff -U0` の hunk ヘッダは ``@@ -a,b +c,d @@ <関数の文脈>``
        という形で、その hunk を囲む関数・クラスの定義行を含む。これを
        直接使うのが要点。

        当初 `-W`（hunk を関数境界まで広げる）で範囲の交差を見ていたが、
        実運用のデータでは交差範囲の中央値が数百行に達し、
        「同じ関数」とは呼べない巨大な領域まで一致扱いになっていた。
        hunk ヘッダの関数名を比べる方が精密で、しかも安い。

        行範囲は **base 側（pre-image）** を返す。異なる PR の範囲を
        比較するには座標系を揃える必要があり、それぞれの PR 自身の
        ベースで取った範囲同士を比べても意味が無い。

        結果は `_hunks` にメモ化する（同じ問いがペアの数だけ繰り返される）。
        """
        key = (base, target, path)
        cached = self._hunks.get(key)
        if cached is not None:
            return cached
        hunks = self._changed_hunks_uncached(base, target, path)
        self._hunks[key] = hunks
        return hunks

    def _changed_hunks_uncached(self, base: str, target: str, path: str) -> list[Hunk]:
        out = self.run("diff", "-U0", "--no-color", base, target, "--", path, check=False)
        hunks: list[Hunk] = []
        for line in out.splitlines():
            if not line.startswith("@@"):
                continue
            m = re.match(r"@@ -(\d+)(?:,(\d+))? \+\d+(?:,\d+)? @@ ?(.*)", line)
            if not m:
                continue
            start = int(m.group(1))
            count = int(m.group(2)) if m.group(2) is not None else 1
            context = m.group(3).strip()
            # count==0 は純粋な追加。挿入位置を 1 行の範囲として扱う。
            end = start + (count if count else 1)
            hunks.append(Hunk(start=start, end=end, context=context))
        return hunks



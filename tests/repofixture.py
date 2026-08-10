"""合成 git リポジトリのビルダ。

干渉レベル L0〜L3 とセマンティック警告のすべてを、実際の git が
再現できる最小のリポジトリとして構築する。層2テストと、
mergetree パーサ用フィクスチャ採取の両方で使う。

再現性のため、author/committer と日時をすべて固定する。
"""

from __future__ import annotations

import subprocess
from pathlib import Path

FIXED_DATE = "2026-01-01T00:00:00+00:00"

ENV = {
    "GIT_AUTHOR_NAME": "fixture",
    "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
    "GIT_COMMITTER_NAME": "fixture",
    "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
    "GIT_AUTHOR_DATE": FIXED_DATE,
    "GIT_COMMITTER_DATE": FIXED_DATE,
    # ロケール依存の出力を避ける
    "LC_ALL": "C",
    "LANG": "C",
}


def _long_function(name: str, body_line: str, pad: int) -> str:
    """行数を稼ぐための関数。pad 行のダミー本体を持つ。"""
    lines = [f"def {name}(x):"]
    lines.append(f"    {body_line}")
    lines.extend(f"    x = x + {i}  # pad" for i in range(pad))
    lines.append("    return x")
    return "\n".join(lines) + "\n"


class RepoFixture:
    def __init__(self, root: Path):
        self.root = root

    def git(self, *args: str, check: bool = True) -> str:
        import os

        env = dict(os.environ)
        env.update(ENV)
        cp = subprocess.run(
            ["git", *args],
            cwd=self.root,
            env=env,
            capture_output=True,
            text=True,
        )
        if check and cp.returncode != 0:
            raise RuntimeError(f"git {' '.join(args)} failed: {cp.stderr}")
        return cp.stdout

    def write(self, path: str, content: str) -> None:
        p = self.root / path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)

    def commit(self, message: str) -> str:
        self.git("add", "-A")
        self.git("commit", "--no-gpg-sign", "-q", "-m", message)
        return self.git("rev-parse", "HEAD").strip()

    def branch_from(self, name: str, start: str = "main") -> None:
        self.git("checkout", "-q", "-b", name, start)

    def rev(self, ref: str) -> str:
        return self.git("rev-parse", ref).strip()


def build(root: Path) -> RepoFixture:
    """干渉シナリオを網羅した合成リポジトリを構築する。

    返り値のリポジトリは `main` を統合ラインとし、以下のブランチを持つ:

      a  : other.py のみ変更           -> b と L0（ファイル重複なし）
      b  : shared.py の先頭側を変更     -> 比較の基準
      c  : shared.py の末尾側を変更     -> b と L1（同一ファイル・別領域）
      d  : shared.py の先頭側を別内容   -> b と L2（テキスト衝突）
      e  : new.py を追加               -> f と L3 (add/add)
      f  : new.py を別内容で追加
      g  : base.py を削除              -> h と L3 (modify/delete)
      h  : base.py を変更
      i  : base.py を renamed.py に改名 -> g と L3 (rename/delete)
      k  : head_fn の 1 行目を変更      -> l と L1 + SAME_FUNCTION_REGION
      l  : head_fn の 3 行目を変更
      m  : requirements.txt の先頭を変更 -> n と L1 + DEPENDENCY_OR_CONFIG_OVERLAP
      n  : requirements.txt の末尾を変更
    """
    root.mkdir(parents=True, exist_ok=True)
    r = RepoFixture(root)
    r.git("init", "-q", "-b", "main")
    r.git("config", "user.name", "fixture")
    r.git("config", "user.email", "fixture@example.invalid")
    r.git("config", "commit.gpgsign", "false")

    # --- main ---------------------------------------------------------
    # shared.py は 2 つの関数を離して配置し、L1（別領域）を作れるようにする
    shared = (
        _long_function("head_fn", "head = 0", 40)
        + "\n\n"
        + _long_function("tail_fn", "tail = 0", 40)
    )
    r.write("shared.py", shared)
    # base.py は「片方が衝突し、もう片方が自動マージされる」フィクスチャを
    # 作るために、離れた 2 箇所を別々に触れるだけの長さを持たせる。
    r.write("base.py", _long_function("base_fn", "base = 0", 30))
    r.write("other.py", _long_function("other_fn", "other = 0", 5))
    r.write("requirements.txt", "numpy==1.0\ntorch==2.0\npyyaml==6.0\n")
    r.write("notes.md", "# Notes\n\n元の説明\n")
    # コメントだけの衝突を作るための、行として独立したコメント
    r.write(
        "commented.py",
        "# 説明の 1 行目\n"
        "# 説明の 2 行目\n"
        "\n"
        + _long_function("worker", "value = 0", 5),
    )
    r.commit("main: initial")

    def variant(branch: str, path: str, old: str, new: str, msg: str) -> None:
        r.branch_from(branch)
        text = (root / path).read_text()
        assert old in text, f"{old!r} not in {path}"
        r.write(path, text.replace(old, new, 1))
        r.commit(msg)

    # a: 別ファイルのみ -> b と L0
    variant("a", "other.py", "other = 0", "other = 111", "a: touch other.py")

    # b / c / d: shared.py
    variant("b", "shared.py", "head = 0", "head = 222", "b: shared head")
    variant("c", "shared.py", "tail = 0", "tail = 333", "c: shared tail")
    variant("d", "shared.py", "head = 0", "head = 444", "d: shared head (conflicting)")

    # e / f: add/add
    r.branch_from("e")
    r.write("new.py", "value = 'from e'\n")
    r.commit("e: add new.py")
    r.branch_from("f")
    r.write("new.py", "value = 'from f'\n")
    r.commit("f: add new.py differently")

    # g: delete base.py / h: modify base.py -> modify/delete
    r.branch_from("g")
    r.git("rm", "-q", "base.py")
    r.commit("g: delete base.py")
    variant("h", "base.py", "base = 0", "base = 555", "h: modify base.py")

    # i: rename base.py -> g と rename/delete
    r.branch_from("i")
    r.git("mv", "base.py", "renamed.py")
    r.commit("i: rename base.py")

    # k / l: 同一関数の別の行 -> L1 + SAME_FUNCTION_REGION
    variant("k", "shared.py", "head = 0", "head = 666", "k: head_fn line 1")
    variant("l", "shared.py", "x = x + 2  # pad", "x = x + 2  # touched by l", "l: head_fn line 3")

    # m / n: 依存ファイルの別の行 -> L1 + DEPENDENCY_OR_CONFIG_OVERLAP
    variant("m", "requirements.txt", "numpy==1.0", "numpy==1.9", "m: bump numpy")
    variant("n", "requirements.txt", "pyyaml==6.0", "pyyaml==6.1", "n: bump pyyaml")

    # u / v: 1 つのファイルが自動マージされ、もう 1 つが衝突する組み合わせ。
    # merge-tree の情報節に Auto-merging と CONFLICT が混在する出力を作るため。
    r.branch_from("u")
    t = (root / "shared.py").read_text().replace("head = 0", "head = 777", 1)
    r.write("shared.py", t)
    t = (root / "base.py").read_text().replace("base = 0", "base = 777", 1)
    r.write("base.py", t)
    r.commit("u: shared head + base head")

    r.branch_from("v")
    t = (root / "shared.py").read_text().replace("head = 0", "head = 888", 1)
    r.write("shared.py", t)
    t = (root / "base.py").read_text().replace("x = x + 25  # pad", "x = x + 25  # touched by v", 1)
    r.write("base.py", t)
    r.commit("v: shared head (conflicting) + base tail (auto-merges)")

    # o / p: 行として独立したコメントを別々に書き換える
    #        -> L2（git はマージできない）だが中身はコメントだけ
    variant("o", "commented.py", "# 説明の 1 行目", "# 説明を o が書き換えた", "o: comment")
    variant("p", "commented.py", "# 説明の 1 行目", "# 説明を p が書き換えた", "p: comment")

    # w / x: コード + 末尾コメントの行でぶつかる -> コメントだけとは見なさない
    #        （行にコードが含まれる以上、片側を選ぶ判断はコードの判断になる）
    variant("w", "commented.py", "value = 0", "value = 1  # w が変更", "w: code line")
    variant("x", "commented.py", "value = 0", "value = 2  # x が変更", "x: code line")

    # q / s: 文書ファイルだけの衝突
    r.branch_from("q")
    r.write("notes.md", "# Notes\n\nq の説明\n")
    r.commit("q: docs")
    r.branch_from("s")
    r.write("notes.md", "# Notes\n\ns の説明\n")
    r.commit("s: docs")

    r.git("checkout", "-q", "main")
    return r


if __name__ == "__main__":  # フィクスチャ採取用
    import sys

    build(Path(sys.argv[1]))

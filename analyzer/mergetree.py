"""`git merge-tree --write-tree -z` の出力パーサ。

I/O を持たない純関数として実装する（テストが git に依存しないため）。
実際に git が吐いたバイト列は tests/fixtures/mergetree/*.bin に固定してある。

出力形式（git 2.54 の実バイト列で確認済み）::

    <tree OID> NUL
    [<mode> SP <oid> SP <stage> TAB <path> NUL]*     ← ステージ節（衝突時のみ）
    NUL                                              ← 空レコード＝節の区切り
    [<path数> NUL <path>×N NUL <型> NUL <本文> NUL]* ← 情報節

重要な観測（すべて実測）:

* クリーンなマージで情報節が一切出ないことがある。その場合レコードは
  tree OID ひとつだけで、空区切りレコードも現れない。
* クリーンなマージでも `Auto-merging` レコードが出ることがある。
  つまり情報節は「衝突リスト」ではない。衝突の判定に使ってはならない。
* **型フィールドと本文で分類の粒度が違う。** add/add 衝突は
  型 = ``CONFLICT (contents)`` だが本文 = ``CONFLICT (add/add): ...``。
  したがって型フィールドの文字列一致で add/add は検出できない。
* 代わりに **ステージ番号の集合が構造衝突の信頼できるシグナル**になる:

  ==============  ================================  ======
  ステージ集合    意味                              レベル
  ==============  ================================  ======
  ``{1,2,3}``     base があり双方が変更＝内容衝突   L2
  ``{2,3}``       base が無い＝add/add              L3
  ``{1,2}``       theirs 側が削除                   L3
  ``{1,3}``       ours 側が削除（modify/delete 等） L3
  ==============  ================================  ======

  これはロケールにも git のメッセージ文言にも依存しない。
"""

from __future__ import annotations

from dataclasses import dataclass, field

# 衝突なし / 衝突あり以外の終了コードはエラー。
# 「非ゼロ＝衝突」と扱うと、引数エラー(129)や壊れたオブジェクトが
# 静かに偽の衝突として混入する。実際に開発中これで事故を起こした。
EXIT_CLEAN = 0
EXIT_CONFLICT = 1


class MergeTreeError(RuntimeError):
    """merge-tree が衝突以外の理由で失敗した。"""

    def __init__(self, returncode: int, stderr: str = "") -> None:
        super().__init__(f"git merge-tree failed with exit {returncode}: {stderr.strip()}")
        self.returncode = returncode
        self.stderr = stderr


@dataclass(frozen=True)
class InfoMessage:
    """情報節の 1 レコード。"""

    paths: tuple[str, ...]
    type: str
    message: str

    @property
    def is_conflict(self) -> bool:
        return self.type.startswith("CONFLICT")


@dataclass(frozen=True)
class MergeTreeResult:
    clean: bool
    tree_oid: str
    #: path -> そのパスに存在したステージ番号の集合
    stages: dict[str, frozenset[int]] = field(default_factory=dict)
    messages: tuple[InfoMessage, ...] = ()

    @property
    def conflict_paths(self) -> frozenset[str]:
        """衝突したパス。

        ステージ節と、型が CONFLICT で始まる情報レコードの和集合を取る。
        ステージを一切生成しない衝突（directory/file 等）があるため、
        どちらか一方だけでは取りこぼす。
        """
        paths = set(self.stages)
        for m in self.messages:
            if m.is_conflict:
                paths.update(m.paths)
        return frozenset(paths)

    @property
    def conflict_types(self) -> frozenset[str]:
        return frozenset(m.type for m in self.messages if m.is_conflict)

    def is_structural(self) -> bool:
        """構造衝突（L3）か。

        内容衝突は base(1) と双方(2,3) が揃う。ひとつでも欠けるパスが
        あれば、追加・削除・改名が絡む構造的な衝突である。
        """
        for st in self.stages.values():
            if st != frozenset({1, 2, 3}):
                return True
        # ステージを生成しない構造衝突は型で拾う（内容衝突以外はすべて構造扱い）
        return any(t != "CONFLICT (contents)" for t in self.conflict_types)


def parse(data: bytes, *, clean: bool) -> MergeTreeResult:
    """`-z` 出力をパースする。

    :param data: merge-tree の stdout そのもの
    :param clean: 終了コードが 0 だったか（呼び出し側が判定して渡す）
    """
    if not data:
        raise ValueError("merge-tree の出力が空")

    records = data.split(b"\0")
    # 末尾の NUL による空要素を落とす（レコード区切りの空レコードとは別物）
    if records and records[-1] == b"":
        records.pop()
    if not records:
        raise ValueError("merge-tree の出力にレコードが無い")

    tree_oid = records[0].decode()
    rest = records[1:]

    # 空レコードがステージ節と情報節を分ける。
    # 情報節が無い場合は空レコードごと現れない。
    try:
        sep = rest.index(b"")
        stage_records, info_records = rest[:sep], rest[sep + 1 :]
    except ValueError:
        stage_records, info_records = rest, []

    stages: dict[str, set[int]] = {}
    for rec in stage_records:
        meta, _, path = rec.partition(b"\t")
        parts = meta.split(b" ")
        if len(parts) != 3 or not path:
            raise ValueError(f"ステージ行を解釈できない: {rec!r}")
        stages.setdefault(_decode(path), set()).add(int(parts[2]))

    messages: list[InfoMessage] = []
    i = 0
    while i < len(info_records):
        n = int(info_records[i])
        paths = tuple(_decode(p) for p in info_records[i + 1 : i + 1 + n])
        type_ = info_records[i + 1 + n].decode()
        # 本文は末尾に改行を持つ
        message = info_records[i + 2 + n].decode("utf-8", "replace").rstrip("\n")
        messages.append(InfoMessage(paths=paths, type=type_, message=message))
        i += n + 3

    return MergeTreeResult(
        clean=clean,
        tree_oid=tree_oid,
        stages={p: frozenset(s) for p, s in stages.items()},
        messages=tuple(messages),
    )


def _decode(raw: bytes) -> str:
    """パスをデコードする。

    `-z` ではパスはクオートされないので、そのまま UTF-8 として読む。
    不正なバイト列を持つパスでも解析を止めないよう surrogateescape を使う。
    """
    return raw.decode("utf-8", "surrogateescape")

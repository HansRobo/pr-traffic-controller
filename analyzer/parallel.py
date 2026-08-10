"""並列実行の共通設定。

git 呼び出しはすべて subprocess なので、待っているあいだ GIL は解放される。
**スレッドで足りる**（プロセスプールにすると `Repo` と候補群を pickle して
渡すコストの方が高い）。`merge-tree --write-tree` は loose object を書くが、
git は tmp→rename で書くので同一 object DB への並行書き込みは安全。

**畳み込みは必ず投入順で行うこと。** 完了順に畳むと、同じ landing 件数の
順序が複数あるときに選ばれる順序が実行ごとに変わり、公開 JSON が揺れる。
`imap` は投入順で返すので、原則としてこれを使う。
"""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Iterable, Iterator, TypeVar

T = TypeVar("T")
R = TypeVar("R")


def jobs(cap: int | None = None) -> int:
    """使う並列度。

    CI の runner は 4 vCPU だが開発機は数十コアあることがある。
    **ローカルの計測は CI を代表しない**ので、実測するときは
    `PR_CONFLICT_JOBS` で CI に合わせること。
    """
    env = os.environ.get("PR_CONFLICT_JOBS", "").strip()
    n = int(env) if env.isdigit() and int(env) > 0 else (os.cpu_count() or 1)
    if cap is not None:
        n = min(n, cap)
    return max(1, n)


def imap(fn: Callable[[T], R], items: Iterable[T], *, cap: int | None = None) -> Iterator[R]:
    """`items` を並列に処理し、結果を **投入順** で返す。

    **評価は遅延しない。** 返るのは完成したリストのイテレータで、プールは
    この関数を出るときに閉じている。遅延にすると executor の寿命が
    呼び出し側の消費タイミングに依存してしまう。

    要素が 1 つ以下、または並列度 1 のときはプールを作らずに直列で回す
    （合成リポジトリのテストや小さなラインで無駄なスレッドを作らない）。
    """
    items = list(items)
    n = jobs(cap)
    if n == 1 or len(items) <= 1:
        return iter([fn(x) for x in items])
    with ThreadPoolExecutor(max_workers=min(n, len(items))) as ex:
        # map は投入順に結果を返す。ここを as_completed にしてはいけない。
        return iter(list(ex.map(fn, items)))

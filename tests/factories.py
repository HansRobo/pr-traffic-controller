"""テスト用のモデル組み立て。

`PullRequest` は必須フィールドが 10 個あり、テストが関心を持つのは
たいてい 2〜3 個だけ。各ファイルで穴埋めを手書きすると、モデルに
フィールドが 1 つ増えたときに全箇所が壊れる（実際 5 箇所に散っていた）。

**fixture ではなく引数付きビルダ**なので `conftest.py` ではなくここに置く。
各テストファイルは、自分の読みやすいシグネチャのラッパをこの上に被せてよい
（`test_dag.py` / `test_refspecs.py` がそうしている）。
"""

from __future__ import annotations

from analyzer.model import Candidate, PullRequest


def make_pr(
    number: int,
    *,
    repo: str = "o/r",
    head_repo: str | None = None,
    head_branch: str | None = None,
    base_repo: str | None = None,
    base_branch: str = "main",
    **kw,
) -> PullRequest:
    """`PullRequest` を最小の指定で作る。

    `head_repo` / `base_repo` を省くと `repo` と同じになる（同一リポジトリ内の
    PR という最も普通の形）。`head_oid` は番号から決まるので、テストごとに
    別の OID を書き分けなくてよい。
    """
    return PullRequest(
        repo=repo,
        number=number,
        title=kw.pop("title", f"{repo}#{number}"),
        url=kw.pop("url", f"https://github.com/{repo}/pull/{number}"),
        author=kw.pop("author", "someone"),
        head_repo=head_repo or repo,
        head_branch=head_branch or f"b{number}",
        head_oid=kw.pop("head_oid", f"{number:040d}"),
        base_repo=base_repo or repo,
        base_branch=base_branch,
        **kw,
    )


#: 「渡さなかった」と「明示的に None を渡した」を区別するための番兵。
#: `landing_tree=None` は「ベース衝突」という意味を持つので、
#: 既定値と同一視してはいけない。
_AUTO = object()


def make_candidate(
    number: int,
    *,
    repo: str = "o/r",
    line: str = "main",
    landing_tree=_AUTO,
    **kw,
) -> Candidate:
    """`Candidate` を PR 番号から作る。

    `landing_tree` の既定は番号由来の値。**ベース衝突を作りたいときは
    明示的に `landing_tree=None`** を渡す（`has_base_conflict` がそれで決まる）。
    """
    return Candidate(
        id=f"{repo}#{number}",
        head=kw.pop("head", f"h{number}"),
        line=line,
        landing_tree=f"t{number}" if landing_tree is _AUTO else landing_tree,
        **kw,
    )

"""GitHub からのデータ取得。

`gh` CLI を subprocess で呼ぶ。PyGithub や requests を使わない理由は
認証で、Actions では `gh` が最初から入っていて `GH_TOKEN` だけで動く。
GHES やキーリングの面倒を自前で持つ必要がない。

PR は GraphQL で 1 クエリにまとめる。REST だと mergeable とレビュー状態が
別エンドポイントになり、PR ごとに追加リクエストが要る。
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import time

from . import parallel
from .model import PullRequest, ReviewNote

#: 一時的な失敗を示す文字列。GitHub の GraphQL は、重いページング付き
#: クエリに対してときどき 502 を返す（大きなリポジトリで実測 3 回に 1 回）。
#: 恒久的なエラー（権限不足・存在しないリポジトリ）と混同しないよう、
#: 明示的に列挙したものだけを再試行する。
#:
_TRANSIENT = ("HTTP 502", "HTTP 503", "HTTP 504", "timeout", "timed out",
              "connection reset", "EOF occurred", "TLS handshake")
_RETRIES = 4

#: 並列に投げるようになったので、二次レート制限も再試行の対象にする。
#: これは「速すぎる」という意味の 403 で、待てば必ず通る。ただし
#: **一時的失敗と同じ待ち方では通らない** —— GitHub は最低 1 分空けることを
#: 求めるので、2→4→8 秒の指数待ちでは 4 回すべてが制限ウィンドウの中で
#: 空振りして恒久エラーとして落ちる。別枠で扱う。
_RATE_LIMITED = ("secondary rate limit", "exceeded a rate limit",
                 "abuse detection", "retry your request again later")
_RATE_LIMIT_WAIT = 65.0

#: GraphQL を同時に投げる本数の**プロセス全体での**上限。CPU ではなく
#: レート制限で決める値なのでコア数には連動させない。
#:
#: セマフォにしているのは、`collect` の中だけで並列度を絞っても足りないため
#: —— 複数リポジトリの準備が先読みで同時に走るので、関数ローカルな上限は
#: 「上限 × 走っている準備の数」まで膨らむ。1 トークンに対する同時本数を
#: 抑えたいのだから、上限もトークン（＝プロセス）側に持たせる。
_API_SLOTS = threading.Semaphore(6)

_PR_QUERY = """
query($owner:String!, $name:String!, $endCursor:String) {
  repository(owner:$owner, name:$name) {
    pullRequests(states:OPEN, first:50, after:$endCursor) {
      pageInfo { hasNextPage endCursor }
      nodes {
        number title url isDraft createdAt updatedAt
        additions deletions changedFiles
        author { login avatarUrl }
        mergeable
        reviewDecision
        baseRefName baseRepository { nameWithOwner }
        headRefName headRefOid headRepository { nameWithOwner }
        reviews(last: 20) {
          nodes { author { login } state body url }
        }
        reviewThreads(first: 20) {
          nodes {
            isResolved isOutdated path line
            comments(first: 1) { nodes { author { login } body url } }
          }
        }
      }
    }
  }
}
"""


class GitHubError(RuntimeError):
    pass


#: 退会・削除された作者の表示名。GraphQL は `author` に null を返す。
UNKNOWN_AUTHOR = "(unknown)"


def _login(node: dict) -> str:
    """`author.login`。null 安全。

    既定値を 1 つに固める。以前は `"(unknown)"` と `"?"` の 2 通りがあり、
    どちらが正なのか読み手に判断できなかった。
    """
    return (node.get("author") or {}).get("login") or UNKNOWN_AUTHOR


def _nodes(parent: dict, key: str) -> list[dict]:
    """`parent[key].nodes`。connection が丸ごと null でも空リストを返す。"""
    return (parent.get(key) or {}).get("nodes") or []


def _gh(*args: str) -> str:
    """`gh` を呼ぶ。一時的な失敗は待って再試行する。

    同時本数は `_API_SLOTS` で押さえる。ここで押さえるのは、呼び出し側が
    どこから何本呼んでいても 1 箇所で効かせるため。
    """
    delay = 2.0
    for attempt in range(1, _RETRIES + 1):
        with _API_SLOTS:
            cp = subprocess.run(["gh", *args], capture_output=True, text=True)
        if cp.returncode == 0:
            return cp.stdout
        err = cp.stderr or ""
        rate_limited = any(s in err for s in _RATE_LIMITED)
        if not (rate_limited or any(s in err for s in _TRANSIENT)) or attempt == _RETRIES:
            # 引数をそのまま出すとクエリ本文で埋まるので、要点だけ残す
            head = " ".join(args[:3])
            raise GitHubError(f"gh {head} … failed: {err.strip()[-300:]}")
        wait = _RATE_LIMIT_WAIT if rate_limited else delay
        print(
            f"  {'レート制限' if rate_limited else '一時的な失敗'}のため "
            f"{wait:.0f} 秒待って再試行します（{attempt}/{_RETRIES - 1}）: "
            f"{err.strip()[-80:]}",
            file=sys.stderr,
        )
        time.sleep(wait)
        delay *= 2
    raise GitHubError("unreachable")


def discover_forks(repo: str) -> list[str]:
    """PR を持っている可能性のあるフォークを列挙する。

    `open_issues_count` は issue と PR の合計なので、PR を持つフォークの
    上位集合になる（issue だけのフォークが混じるが、その場合 PR 一覧が
    空で終わるだけ）。取りこぼしは原理的に無い。
    """
    out = _gh(
        "api",
        f"repos/{repo}/forks",
        "--paginate",
        "--jq",
        ".[] | select(.open_issues_count > 0) | .nameWithOwner // .full_name",
    )
    return [line.strip() for line in out.splitlines() if line.strip()]


def fetch_pull_requests(repo: str) -> list[PullRequest]:
    owner, name = repo.split("/", 1)
    out = _gh(
        "api",
        "graphql",
        "--paginate",
        "-F",
        f"owner={owner}",
        "-F",
        f"name={name}",
        "-f",
        f"query={_PR_QUERY}",
    )

    # --paginate は複数の JSON ドキュメントを連結して返すことがある
    nodes: list[dict] = []
    decoder = json.JSONDecoder()
    idx = 0
    text = out.strip()
    while idx < len(text):
        doc, offset = decoder.raw_decode(text, idx)
        nodes.extend(doc["data"]["repository"]["pullRequests"]["nodes"])
        idx = offset
        while idx < len(text) and text[idx] in " \r\n\t":
            idx += 1

    prs: list[PullRequest] = []
    for n in nodes:
        notes = _review_notes(n)
        head_repo = (n.get("headRepository") or {}).get("nameWithOwner")
        base_repo = (n.get("baseRepository") or {}).get("nameWithOwner") or repo
        if not head_repo:
            # head リポジトリが消えている（フォーク削除など）。解析できない。
            continue
        prs.append(
            PullRequest(
                repo=repo,
                number=n["number"],
                title=n["title"],
                url=n["url"],
                author=_login(n),
                author_avatar_url=(n.get("author") or {}).get("avatarUrl", ""),
                head_repo=head_repo,
                head_branch=n["headRefName"],
                head_oid=n["headRefOid"],
                base_repo=base_repo,
                base_branch=n["baseRefName"],
                is_draft=n["isDraft"],
                review_decision=_effective_review_decision(
                    n.get("reviewDecision"),
                    _nodes(n, "reviews"),
                ),
                github_mergeable=n.get("mergeable") or "UNKNOWN",
                additions=n.get("additions") or 0,
                deletions=n.get("deletions") or 0,
                changed_files_count=n.get("changedFiles") or 0,
                created_at=n.get("createdAt") or "",
                updated_at=n.get("updatedAt") or "",
                review_notes=notes,
            )
        )
    return prs


#: コメント本文はそのまま持つと肥大するので切り詰める。
_BODY_LIMIT = 600

#: 承認状態を左右しないレビュー種別。
#: COMMENTED は「見たがどちらでもない」で、以前の承認を取り消さない。
_NEUTRAL_REVIEWS = ("COMMENTED", "PENDING", "DISMISSED")


def _effective_review_decision(declared: str | None, reviews: list[dict]) -> str:
    """レビュー状態を決める。

    GitHub の `reviewDecision` は **レビュー必須の設定があるリポジトリでしか
    算出されない**。フォークのように設定の無いリポジトリでは、承認が
    あっても null が返る。そのままだと承認済みの PR を「レビュー待ち」と
    誤って表示し、マージ順の推奨まで狂う。

    算出されていればそれを信じ、無ければ実際のレビューから決める。
    判定は GitHub の見え方に合わせ、**投稿者ごとの最新のレビュー**を採る
    （COMMENTED は状態を変えない）。
    """
    if declared:
        return declared

    latest: dict[str, str] = {}
    for r in reviews:  # last: N は古い順に並ぶので、後勝ちでよい
        state = r.get("state") or ""
        if state in _NEUTRAL_REVIEWS:
            continue
        latest[_login(r)] = state

    states = set(latest.values())
    if "CHANGES_REQUESTED" in states:
        return "CHANGES_REQUESTED"
    if "APPROVED" in states:
        return "APPROVED"
    return "NONE"


def _review_notes(node: dict) -> tuple[ReviewNote, ...]:
    """レビューの指摘を「まだ直す必要があるもの」に絞って取り出す。

    解決済みスレッドは除く。本文が空のレビュー（承認だけ、など）も除く。
    """
    out: list[ReviewNote] = []

    for r in _nodes(node, "reviews"):
        if (r.get("state") or "") not in ("CHANGES_REQUESTED", "COMMENTED"):
            continue
        body = (r.get("body") or "").strip()
        if not body:
            continue
        out.append(
            ReviewNote(
                author=_login(r),
                state=r.get("state") or "COMMENTED",
                body=body[:_BODY_LIMIT],
                url=r.get("url") or "",
            )
        )

    for th in _nodes(node, "reviewThreads"):
        if th.get("isResolved"):
            continue
        comments = _nodes(th, "comments")
        if not comments:
            continue
        c = comments[0]
        body = (c.get("body") or "").strip()
        if not body:
            continue
        out.append(
            ReviewNote(
                author=_login(c),
                state="INLINE",
                body=body[:_BODY_LIMIT],
                path=th.get("path") or "",
                line=th.get("line"),
                url=c.get("url") or "",
                outdated=bool(th.get("isOutdated")),
            )
        )
    return tuple(out)


def collect(target_repo: str, *, include_forks: bool = True) -> tuple[list[PullRequest], list[str]]:
    """対象リポジトリとそのフォーク群の全オープン PR を集める。

    フォークごとのクエリは独立なので並列に投げる。ただし **返す順序は
    `discover_forks` の順に固定する** —— `forks` はそのまま JSON に出るので、
    完了順に積むと解析結果が実行ごとに揺れる。
    """
    candidates = discover_forks(target_repo) if include_forks else []
    # 対象リポジトリも同じプールに入れて、フォークの待ちと重ねる。
    # 同時本数の上限は `_gh` 側（`_API_SLOTS`）で効かせる。
    repos = [target_repo, *candidates]
    results = list(parallel.imap(fetch_pull_requests, repos))

    prs = list(results[0])
    forks: list[str] = []
    for fork, fork_prs in zip(candidates, results[1:]):
        if fork_prs:
            forks.append(fork)
            prs.extend(fork_prs)
    return prs, forks

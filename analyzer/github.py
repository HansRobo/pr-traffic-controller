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

from .model import PullRequest

_PR_QUERY = """
query($owner:String!, $name:String!, $endCursor:String) {
  repository(owner:$owner, name:$name) {
    pullRequests(states:OPEN, first:100, after:$endCursor) {
      pageInfo { hasNextPage endCursor }
      nodes {
        number title url isDraft createdAt updatedAt
        additions deletions changedFiles
        author { login }
        mergeable
        reviewDecision
        baseRefName baseRepository { nameWithOwner }
        headRefName headRefOid headRepository { nameWithOwner }
      }
    }
  }
}
"""


class GitHubError(RuntimeError):
    pass


def _gh(*args: str) -> str:
    cp = subprocess.run(["gh", *args], capture_output=True, text=True)
    if cp.returncode != 0:
        raise GitHubError(f"gh {' '.join(args)} failed:\n{cp.stderr}")
    return cp.stdout


def repo_info(repo: str) -> dict:
    return json.loads(_gh("api", f"repos/{repo}"))


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
                author=(n.get("author") or {}).get("login", "(unknown)"),
                head_repo=head_repo,
                head_branch=n["headRefName"],
                head_oid=n["headRefOid"],
                base_repo=base_repo,
                base_branch=n["baseRefName"],
                is_draft=n["isDraft"],
                review_decision=n.get("reviewDecision") or "NONE",
                github_mergeable=n.get("mergeable") or "UNKNOWN",
                additions=n.get("additions") or 0,
                deletions=n.get("deletions") or 0,
                changed_files_count=n.get("changedFiles") or 0,
                created_at=n.get("createdAt") or "",
                updated_at=n.get("updatedAt") or "",
            )
        )
    return prs


def collect(target_repo: str, *, include_forks: bool = True) -> tuple[list[PullRequest], list[str]]:
    """対象リポジトリとそのフォーク群の全オープン PR を集める。"""
    prs = fetch_pull_requests(target_repo)
    forks: list[str] = []
    if include_forks:
        for fork in discover_forks(target_repo):
            fork_prs = fetch_pull_requests(fork)
            if fork_prs:
                forks.append(fork)
                prs.extend(fork_prs)
    return prs, forks

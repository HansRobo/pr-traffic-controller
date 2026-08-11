"""fetch する ref の組み立て。

ワイルドカード（`refs/pull/*/head`）で全件取るのをやめて、open PR の分だけを
名指しで取るようにしている。実測で対象リポジトリには 3310 件の PR ref と
790 件のブランチがあり、必要なのは 134 件だった。

**ここを取り違えると、エラーにならずに PR が解析から落ちる。** head OID が
ローカルに無い PR は「head コミットがローカルに存在しない」として静かに
skipped になるだけなので、規則をテストで固定しておく。
"""

from __future__ import annotations

from analyzer.analyze import _refspecs
from analyzer.model import PullRequest

from .factories import make_pr


def pr(number: int, *, base_repo: str, head_repo: str, base_branch: str = "main") -> PullRequest:
    """refspec の検証に要るのは repo と番号だけ。他は固定でよい。"""
    return make_pr(
        number,
        repo=base_repo,
        head_repo=head_repo,
        head_branch="feature",
        head_oid="0" * 40,
        base_branch=base_branch,
    )


TARGET = "owner/app"
FORK = "contrib/app"
ALL_BRANCHES = frozenset({"main", "develop", "release"})


def specs_for(repo_name: str, prs: list[PullRequest], *, lines: list[str] | None = None):
    ns = repo_name.split("/")[0].lower()
    return _refspecs(TARGET, prs, repo_name, ns, lines or [], ALL_BRANCHES)


class TestPullRefsComeFromTheBaseRepository:
    """`refs/pull/<n>/head` は base 側のリポジトリにしか無い。"""

    def test_cross_repository_pr_is_fetched_from_the_target(self):
        """head がフォークにある PR も、base 側（対象）から取る。

        これを head_repo で引くと、フォークに存在しない ref を要求しつつ
        必要な head OID を取り逃す —— 最も静かな壊れ方をする。
        """
        prs = [pr(7, base_repo=TARGET, head_repo=FORK)]
        assert "+refs/pull/7/head:refs/remotes/owner-pr/7" in specs_for(TARGET, prs)

    def test_fork_is_not_asked_for_a_pr_it_does_not_host(self):
        prs = [pr(7, base_repo=TARGET, head_repo=FORK)]
        assert not any("refs/pull/" in s for s in specs_for(FORK, prs))

    def test_pr_内部完結_is_fetched_from_the_fork(self):
        """フォーク内で完結する PR は、そのフォークから取る。"""
        prs = [pr(3, base_repo=FORK, head_repo=FORK, base_branch="develop")]
        specs = specs_for(FORK, prs)
        assert "+refs/pull/3/head:refs/remotes/contrib-pr/3" in specs
        assert "+refs/heads/develop:refs/remotes/contrib-br/develop" in specs

    def test_target_does_not_fetch_the_forks_pr_number(self):
        prs = [pr(3, base_repo=FORK, head_repo=FORK)]
        assert not any("refs/pull/" in s for s in specs_for(TARGET, prs))


class TestBranchRefs:
    def test_target_branches_land_in_origin(self):
        """`branch_ref()` は対象のブランチを `origin/<name>` で引く。"""
        prs = [pr(1, base_repo=TARGET, head_repo=TARGET, base_branch="develop")]
        assert "+refs/heads/develop:refs/remotes/origin/develop" in specs_for(TARGET, prs)

    def test_integration_lines_are_included_even_without_prs(self):
        assert "+refs/heads/release:refs/remotes/origin/release" in specs_for(
            TARGET, [], lines=["release"]
        )

    def test_missing_branches_are_skipped(self):
        """存在しないブランチを名指しすると fetch 全体が失敗する。

        ワイルドカードは黙って飛ばしていたので、絞り込みで挙動を悪化させない。
        """
        prs = [pr(1, base_repo=TARGET, head_repo=TARGET, base_branch="deleted-branch")]
        specs = specs_for(TARGET, prs, lines=["also-gone"])
        assert not any("refs/heads/" in s for s in specs)
        assert "+refs/pull/1/head:refs/remotes/owner-pr/1" in specs, "PR ref の方は残る"

    def test_branches_are_deduplicated_and_sorted(self):
        prs = [
            pr(1, base_repo=TARGET, head_repo=TARGET, base_branch="main"),
            pr(2, base_repo=TARGET, head_repo=TARGET, base_branch="main"),
            pr(3, base_repo=TARGET, head_repo=TARGET, base_branch="develop"),
        ]
        branches = [s for s in specs_for(TARGET, prs, lines=["main"]) if "refs/heads/" in s]
        assert branches == [
            "+refs/heads/develop:refs/remotes/origin/develop",
            "+refs/heads/main:refs/remotes/origin/main",
        ]

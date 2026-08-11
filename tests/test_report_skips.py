"""解析から外した PR（`Skip`）が report にどう出るか。

**この層は一度壊れている。** かつて `report` は除外理由の日本語散文を
`"含まれていない" in reason` で振り分け、`reason.split("'")[1]` で
ブランチ名を取り出していた。文面を 1 語変えるだけで「指定し忘れた統合ライン」の
警告が黙って消える状態だったので、`model.Skip` に `kind` / `branch` / `pr_count`
を持たせて構造で運ぶ形に変えた。

そこでここでは **`reason` の文面に一切依存しないこと**を検証する。
文面を空にしても警告が出る、というのが要点。

`report.build` は `repo` を統合ライン同士の関係を出すときにしか使わないので、
`line_names` が 1 本なら `repo=None` で呼べる（実 git もフェイクも要らない）。
"""

from __future__ import annotations

import pytest

from analyzer import dag, report
from analyzer.model import PullRequest, Skip


def mk_pr(n: int, *, base_branch: str = "main") -> PullRequest:
    return PullRequest(
        repo="o/r",
        number=n,
        title=f"#{n}",
        url="",
        author="a",
        head_repo="o/r",
        head_branch=f"b{n}",
        head_oid=f"{n:040d}",
        base_repo="o/r",
        base_branch=base_branch,
    )


def build_report(skipped: list[Skip], prs: list[PullRequest] | None = None) -> dict:
    prs = prs if prs is not None else [mk_pr(1)]
    graph = dag.build(prs, {dag.node_key("o/r", "main"): "main"})
    return report.build(
        target="o/r",
        forks=[],
        git_version="2.43.0",
        graph=graph,
        line_names=["main"],
        line_oids={"main": "0" * 40},
        candidates={"main": []},
        pairs={"main": []},
        orders={},
        skipped=skipped,
        file_changes={},
        duration=0.1,
        repo=None,
    )


def actions_of(out: dict, kind: str) -> list[dict]:
    return [a for a in out["actions"] if a["kind"] == kind]


class TestExclusionWarnings:
    """除外された PR は必ず warnings に出る（黙って消えない）。"""

    def test_every_skip_becomes_a_warning(self):
        out = build_report([
            Skip("o/r#1", "統合ラインを解決できない (no_root)"),
            Skip("o/r#2", "head コミットがローカルに存在しない"),
        ])
        warns = [w for w in out["warnings"] if w["kind"] == "pr_excluded"]
        assert [w["subjects"] for w in warns] == [["o/r#1"], ["o/r#2"]]

    def test_reason_is_passed_through_verbatim(self):
        """`reason` は人向けの文面としてそのまま出る（振り分けには使わない）。"""
        out = build_report([Skip("o/r#1", "merge-tree エラー: 何か")])
        (w,) = [w for w in out["warnings"] if w["kind"] == "pr_excluded"]
        assert w["detail"] == "merge-tree エラー: 何か"
        assert w["severity"] == "warn"

    def test_no_skips_means_no_exclusion_warnings(self):
        out = build_report([])
        assert [w for w in out["warnings"] if w["kind"] == "pr_excluded"] == []


class TestUnlistedLineAction:
    """指定し忘れた統合ラインは行動可能な指示として前面に出す。

    黙って除外すると「衝突 0 件」という誤った安心を与えるため。
    """

    def test_kind_drives_the_action_not_the_wording(self):
        """**回帰テストの本体。** `reason` が空でも警告は出る。"""
        out = build_report([
            Skip(
                "o/r#1",
                reason="",  # 文面をすべて奪う
                kind="unlisted_line",
                branch="dev",
                pr_count=3,
            )
        ])
        (a,) = actions_of(out, "unlisted_integration_line")
        assert a["branch"] == "dev"
        assert a["prs"] == ["o/r#1"]

    def test_branch_comes_from_the_field_not_from_quotes_in_the_reason(self):
        """`reason` 中のクオートに引きずられない。

        旧実装は `reason.split("'")[1]` を見ていたので、この文面だと
        'まちがい' を拾ってしまう。
        """
        out = build_report([
            Skip(
                "o/r#1",
                reason="'まちがい' というブランチ名がクオートで入っている",
                kind="unlisted_line",
                branch="dev",
                pr_count=1,
            )
        ])
        (a,) = actions_of(out, "unlisted_integration_line")
        assert a["branch"] == "dev"

    def test_unresolved_skips_do_not_create_the_action(self):
        """既定の `kind`（unresolved）は行動指示にしない。"""
        out = build_report([
            Skip("o/r#1", "'dev' という語を含むが unlisted_line ではない"),
        ])
        assert actions_of(out, "unlisted_integration_line") == []

    def test_prs_are_grouped_by_branch(self):
        out = build_report(
            [
                Skip("o/r#1", "", kind="unlisted_line", branch="dev", pr_count=2),
                Skip("o/r#2", "", kind="unlisted_line", branch="dev", pr_count=2),
                Skip("o/r#3", "", kind="unlisted_line", branch="release", pr_count=1),
            ],
            prs=[mk_pr(n) for n in (1, 2, 3)],
        )
        acts = actions_of(out, "unlisted_integration_line")
        assert [(a["branch"], a["pr_count"], a["prs"]) for a in acts] == [
            ("dev", 2, ["o/r#1", "o/r#2"]),
            ("release", 1, ["o/r#3"]),
        ]

    def test_busiest_branch_comes_first(self):
        """件数の多い順。読み手が最初に見るべきものを先頭に置く。"""
        out = build_report(
            [
                Skip("o/r#1", "", kind="unlisted_line", branch="quiet", pr_count=1),
                Skip("o/r#2", "", kind="unlisted_line", branch="busy", pr_count=2),
                Skip("o/r#3", "", kind="unlisted_line", branch="busy", pr_count=2),
            ],
            prs=[mk_pr(n) for n in (1, 2, 3)],
        )
        acts = actions_of(out, "unlisted_integration_line")
        assert [a["branch"] for a in acts] == ["busy", "quiet"]

    def test_message_names_the_branch(self):
        """文面自体は人向けなので、ブランチ名が入っていることだけ確かめる。"""
        out = build_report([
            Skip("o/r#1", "", kind="unlisted_line", branch="dev", pr_count=1)
        ])
        (a,) = actions_of(out, "unlisted_integration_line")
        assert "dev" in a["message"]


class TestSkipDefaults:
    def test_kind_defaults_to_unresolved(self):
        assert Skip("o/r#1", "理由").kind == "unresolved"

    def test_is_frozen(self):
        s = Skip("o/r#1", "理由")
        with pytest.raises(Exception):
            s.pr_id = "o/r#2"  # type: ignore[misc]

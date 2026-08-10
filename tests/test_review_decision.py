"""レビュー状態の算出。

GitHub の `reviewDecision` はレビュー必須の設定があるリポジトリでしか
算出されない。フォークでは承認があっても null になるため、実際の
レビューから決め直す必要がある。
"""

from analyzer.github import _effective_review_decision as decide


def rv(login, state):
    return {"author": {"login": login}, "state": state}


class TestDeclared:
    def test_declared_value_wins(self):
        assert decide("APPROVED", []) == "APPROVED"
        assert decide("CHANGES_REQUESTED", [rv("a", "APPROVED")]) == "CHANGES_REQUESTED"


class TestDerived:
    def test_approval_without_declared(self):
        """フォークで起きる実例。承認があるのに null が返る。"""
        assert decide(None, [rv("kosuke55", "APPROVED")]) == "APPROVED"

    def test_changes_requested_wins_over_approval(self):
        assert decide(None, [rv("a", "APPROVED"), rv("b", "CHANGES_REQUESTED")]) == "CHANGES_REQUESTED"

    def test_latest_review_per_author(self):
        """同じ人が出し直したら新しい方を採る。"""
        assert decide(None, [rv("a", "CHANGES_REQUESTED"), rv("a", "APPROVED")]) == "APPROVED"
        assert decide(None, [rv("a", "APPROVED"), rv("a", "CHANGES_REQUESTED")]) == "CHANGES_REQUESTED"

    def test_comment_does_not_cancel_approval(self):
        assert decide(None, [rv("a", "APPROVED"), rv("a", "COMMENTED")]) == "APPROVED"

    def test_dismissed_is_ignored(self):
        assert decide(None, [rv("a", "APPROVED"), rv("a", "DISMISSED")]) == "APPROVED"

    def test_only_comments(self):
        assert decide(None, [rv("a", "COMMENTED")]) == "NONE"

    def test_no_reviews(self):
        assert decide(None, []) == "NONE"

    def test_missing_author_does_not_crash(self):
        assert decide(None, [{"state": "APPROVED"}]) == "APPROVED"

"""並列実行の共通設定。

`imap` が **投入順** で返すことは、このパッケージの決定性の土台である。
`analyze` の着地tree収集・`interference` の hunk 温め・`github` の fork 走査・
`filechanges.build` がすべてこれに乗っており、崩れると公開 JSON が実行ごとに
揺れる（`filechanges` に至っては PR と hunk を静かに取り違える）。
それだけの前提が長らく無検証だったので、ここで押さえる。

**並列度を明示すること。** `imap` は並列度 1 や要素 1 個以下ではプールを作らず
直列に回るので、`PR_CONFLICT_JOBS` を指定しないテストは順序を検証したことに
ならない（どんな実装でも緑になる）。
"""

from __future__ import annotations

import time

import pytest

from analyzer import parallel


class TestSubmissionOrder:
    def test_results_follow_submission_order(self, monkeypatch):
        """完了順が投入順と逆でも、返るのは投入順。

        先頭をいちばん遅くするので、完了順に畳む実装（`as_completed`）なら
        0 が最後に来て落ちる。
        """
        monkeypatch.setenv("PR_CONFLICT_JOBS", "8")

        def slow(i: int) -> int:
            time.sleep(0.05 if i < 4 else 0.0)
            return i

        assert list(parallel.imap(slow, list(range(8)))) == list(range(8))

    def test_order_holds_when_pool_is_smaller_than_input(self, monkeypatch):
        """作業数がワーカ数を超え、複数の波に分かれても投入順が保たれる。"""
        monkeypatch.setenv("PR_CONFLICT_JOBS", "2")

        def slow(i: int) -> int:
            # 波ごとに先頭を遅らせる
            time.sleep(0.03 if i % 2 == 0 else 0.0)
            return i

        assert list(parallel.imap(slow, list(range(10)))) == list(range(10))

    def test_consumes_iterables_not_just_sequences(self, monkeypatch):
        """`items` はイテレータでもよい（内部で list 化される）。"""
        monkeypatch.setenv("PR_CONFLICT_JOBS", "4")
        assert list(parallel.imap(lambda x: x, iter(range(6)))) == list(range(6))

    def test_exception_propagates(self, monkeypatch):
        """1 件の失敗を握り潰さない（黙って欠けた結果を返さない）。"""
        monkeypatch.setenv("PR_CONFLICT_JOBS", "4")

        def boom(i: int) -> int:
            if i == 2:
                raise RuntimeError("失敗")
            return i

        with pytest.raises(RuntimeError):
            list(parallel.imap(boom, list(range(5))))


class TestSerialFallback:
    """要素が少ない・並列度 1 のときはプールを作らない。

    ここを通ると順序は自明に保たれる。**上の順序テストがこの経路に
    落ちてしまうと、実装が壊れていても緑になる**ので、分岐の境目を明示しておく。
    """

    def test_single_item(self, monkeypatch):
        monkeypatch.setenv("PR_CONFLICT_JOBS", "8")
        assert list(parallel.imap(lambda x: x * 2, [3])) == [6]

    def test_empty(self, monkeypatch):
        monkeypatch.setenv("PR_CONFLICT_JOBS", "8")
        assert list(parallel.imap(lambda x: x, [])) == []

    def test_jobs_one_still_returns_everything(self, monkeypatch):
        monkeypatch.setenv("PR_CONFLICT_JOBS", "1")
        assert list(parallel.imap(lambda x: x, list(range(5)))) == list(range(5))


class TestJobs:
    """`PR_CONFLICT_JOBS` の解釈。

    CI は 4 vCPU、開発機は数十コアありうるので、実測を CI に合わせるための
    つまみ。壊れると「ローカルでは速いが CI で違う挙動」になる。
    """

    def test_env_overrides_cpu_count(self, monkeypatch):
        monkeypatch.setenv("PR_CONFLICT_JOBS", "3")
        assert parallel.jobs() == 3

    @pytest.mark.parametrize("value", ["", "  ", "0", "-2", "abc", "2.5"])
    def test_invalid_values_fall_back_to_cpu_count(self, monkeypatch, value):
        """不正値で 0 や例外にしない。0 だとプールが作れず解析が止まる。"""
        monkeypatch.setenv("PR_CONFLICT_JOBS", value)
        assert parallel.jobs() >= 1

    def test_cap_limits_the_result(self, monkeypatch):
        monkeypatch.setenv("PR_CONFLICT_JOBS", "16")
        assert parallel.jobs(cap=4) == 4

    def test_cap_does_not_raise_the_result(self, monkeypatch):
        monkeypatch.setenv("PR_CONFLICT_JOBS", "2")
        assert parallel.jobs(cap=8) == 2

    def test_never_returns_zero(self, monkeypatch):
        monkeypatch.setenv("PR_CONFLICT_JOBS", "1")
        assert parallel.jobs(cap=0) == 1

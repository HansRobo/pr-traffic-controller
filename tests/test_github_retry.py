"""一時的な失敗の再試行。

GitHub の GraphQL は、大きなリポジトリへのページング付きクエリに対して
ときどき 502 を返す。恒久的なエラーと混同せず、一時的なものだけを
再試行する。
"""

from __future__ import annotations

import subprocess

import pytest

from analyzer import github


class Result:
    def __init__(self, code, out="", err=""):
        self.returncode, self.stdout, self.stderr = code, out, err


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(github.time, "sleep", lambda *_: None)


def make_runner(results):
    calls = []

    def run(args, **kw):
        calls.append(args)
        return results[min(len(calls) - 1, len(results) - 1)]

    run.calls = calls
    return run


class TestRetry:
    def test_succeeds_without_retry(self, monkeypatch):
        run = make_runner([Result(0, "ok")])
        monkeypatch.setattr(subprocess, "run", run)
        assert github._gh("api", "x") == "ok"
        assert len(run.calls) == 1

    def test_retries_transient_then_succeeds(self, monkeypatch):
        run = make_runner([Result(1, err="gh: HTTP 502"), Result(0, "ok")])
        monkeypatch.setattr(subprocess, "run", run)
        assert github._gh("api", "x") == "ok"
        assert len(run.calls) == 2

    @pytest.mark.parametrize("err", ["gh: HTTP 502", "HTTP 503", "HTTP 504",
                                     "i/o timeout", "connection reset by peer"])
    def test_transient_kinds(self, monkeypatch, err):
        run = make_runner([Result(1, err=err), Result(0, "ok")])
        monkeypatch.setattr(subprocess, "run", run)
        assert github._gh("api", "x") == "ok"

    def test_permanent_error_is_not_retried(self, monkeypatch):
        """権限不足や不存在は何度試しても同じなので、すぐ諦める。"""
        run = make_runner([Result(1, err="gh: Could not resolve to a Repository")])
        monkeypatch.setattr(subprocess, "run", run)
        with pytest.raises(github.GitHubError):
            github._gh("api", "x")
        assert len(run.calls) == 1

    def test_gives_up_after_limit(self, monkeypatch):
        run = make_runner([Result(1, err="gh: HTTP 502")])
        monkeypatch.setattr(subprocess, "run", run)
        with pytest.raises(github.GitHubError):
            github._gh("api", "x")
        assert len(run.calls) == github._RETRIES

    def test_error_message_stays_short(self, monkeypatch):
        """クエリ本文でログが埋まらないよう、要点だけ残す。"""
        run = make_runner([Result(1, err="x" * 5000)])
        monkeypatch.setattr(subprocess, "run", run)
        with pytest.raises(github.GitHubError) as e:
            github._gh("api", "graphql", "-F", "owner=x", "-f", "query=" + "Q" * 4000)
        assert len(str(e.value)) < 500

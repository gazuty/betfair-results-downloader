"""
Slack notifier retry.

slack_notify carries the pipeline's failure alerts: the 2026-08-30/31
incidents were only visible because a Slack message got through. A single
dropped packet or a rate-limit response must not silence the very message
that reports a problem, so transient failures retry with backoff and 429
honours Retry-After.
"""

from __future__ import annotations

import io
import json
import urllib.error

import pytest

from betfair_results_downloader import slack_notify


class _FakeResponse:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


_OK_BODY = json.dumps({"ok": True, "ts": "123.456"}).encode()


def _configure(monkeypatch) -> dict:
    monkeypatch.setattr(slack_notify, "_read_local_config", lambda: None)
    return {"slack": {"bot_token": "xoxb-test", "channel": "C123", "enabled": True}}


def _http_error(code: int, headers: dict | None = None) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        slack_notify._SLACK_POST_URL, code, "err", headers or {}, io.BytesIO(b"")
    )


def test_transient_network_error_is_retried(monkeypatch) -> None:
    creds = _configure(monkeypatch)
    calls: list[int] = []
    sleeps: list[float] = []

    def urlopen(req, timeout=None):
        calls.append(1)
        if len(calls) < 3:
            raise urllib.error.URLError(ConnectionResetError("reset"))
        return _FakeResponse(_OK_BODY)

    monkeypatch.setattr(slack_notify.urllib.request, "urlopen", urlopen)

    ts = slack_notify.post_message("hello", creds, sleep=sleeps.append)

    assert ts == "123.456"
    assert len(calls) == 3
    assert sleeps == [2.0, 4.0]


def test_429_honours_retry_after(monkeypatch) -> None:
    creds = _configure(monkeypatch)
    calls: list[int] = []
    sleeps: list[float] = []

    def urlopen(req, timeout=None):
        calls.append(1)
        if len(calls) == 1:
            raise _http_error(429, {"Retry-After": "7"})
        return _FakeResponse(_OK_BODY)

    monkeypatch.setattr(slack_notify.urllib.request, "urlopen", urlopen)

    assert slack_notify.post_message("hello", creds, sleep=sleeps.append) == "123.456"
    assert sleeps == [7.0]


def test_read_timeout_is_retried(monkeypatch) -> None:
    """A read timeout after connect escapes URLError's wrap as TimeoutError."""
    creds = _configure(monkeypatch)
    calls: list[int] = []

    def urlopen(req, timeout=None):
        calls.append(1)
        if len(calls) == 1:
            raise TimeoutError("timed out")
        return _FakeResponse(_OK_BODY)

    monkeypatch.setattr(slack_notify.urllib.request, "urlopen", urlopen)

    assert slack_notify.post_message("hello", creds, sleep=lambda _s: None) == "123.456"
    assert len(calls) == 2


def test_garbled_body_is_retried(monkeypatch) -> None:
    """A truncated body from a proxy or dying connection is transient."""
    creds = _configure(monkeypatch)
    calls: list[int] = []

    def urlopen(req, timeout=None):
        calls.append(1)
        if len(calls) == 1:
            return _FakeResponse(b'{"ok": tru')
        return _FakeResponse(_OK_BODY)

    monkeypatch.setattr(slack_notify.urllib.request, "urlopen", urlopen)

    assert slack_notify.post_message("hello", creds, sleep=lambda _s: None) == "123.456"
    assert len(calls) == 2


def test_client_error_is_not_retried(monkeypatch) -> None:
    """A 4xx means the request is wrong; repeating it will not change that."""
    creds = _configure(monkeypatch)
    calls: list[int] = []

    def urlopen(req, timeout=None):
        calls.append(1)
        raise _http_error(400)

    monkeypatch.setattr(slack_notify.urllib.request, "urlopen", urlopen)

    with pytest.raises(RuntimeError, match="Slack request failed"):
        slack_notify.post_message("hello", creds, sleep=lambda _s: None)
    assert len(calls) == 1


def test_api_level_error_is_not_retried(monkeypatch) -> None:
    """ok=false is Slack answering clearly (bad channel, bad token)."""
    creds = _configure(monkeypatch)
    calls: list[int] = []

    def urlopen(req, timeout=None):
        calls.append(1)
        return _FakeResponse(
            json.dumps({"ok": False, "error": "channel_not_found"}).encode()
        )

    monkeypatch.setattr(slack_notify.urllib.request, "urlopen", urlopen)

    with pytest.raises(RuntimeError, match="channel_not_found"):
        slack_notify.post_message("hello", creds, sleep=lambda _s: None)
    assert len(calls) == 1


def test_exhausted_attempts_raise_with_last_error(monkeypatch) -> None:
    creds = _configure(monkeypatch)
    calls: list[int] = []
    sleeps: list[float] = []

    def urlopen(req, timeout=None):
        calls.append(1)
        raise _http_error(503)

    monkeypatch.setattr(slack_notify.urllib.request, "urlopen", urlopen)

    with pytest.raises(RuntimeError, match="after 3 attempts"):
        slack_notify.post_message("hello", creds, sleep=sleeps.append)
    assert len(calls) == 3
    assert sleeps == [2.0, 4.0]


def test_unconfigured_never_touches_the_network(monkeypatch) -> None:
    monkeypatch.setattr(slack_notify, "_read_local_config", lambda: None)

    def urlopen(req, timeout=None):  # pragma: no cover - must not be called
        raise AssertionError("network touched while unconfigured")

    monkeypatch.setattr(slack_notify.urllib.request, "urlopen", urlopen)

    with pytest.raises(slack_notify.SlackNotConfigured):
        slack_notify.post_message("hello", {"slack": {}})


def test_unparsable_retry_after_falls_back_to_backoff(monkeypatch) -> None:
    creds = _configure(monkeypatch)
    calls: list[int] = []
    sleeps: list[float] = []

    def urlopen(req, timeout=None):
        calls.append(1)
        if len(calls) == 1:
            raise _http_error(429, {"Retry-After": "soon"})
        return _FakeResponse(_OK_BODY)

    monkeypatch.setattr(slack_notify.urllib.request, "urlopen", urlopen)

    assert slack_notify.post_message("hello", creds, sleep=sleeps.append) == "123.456"
    assert sleeps == [2.0]

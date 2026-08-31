"""
The shared transient-failure policy for Betfair calls.

Live evidence behind every case here: the 2026-08-31 09:00 run died on one
un-retried cert-SSO read timeout, 2026-07-19 on a connection reset, and
StatusCodeError (HTTP 429/503) subclasses BetfairError rather than APIError,
so the old ``except APIError`` retries could never see a rate limit at all.
"""

from __future__ import annotations

import pytest
from betfairlightweight.exceptions import APIError, StatusCodeError

from betfair_results_downloader.betfair_net import (
    is_transient_betfair_error,
    retry_betfair_call,
)


def _api_error(msg: str) -> APIError:
    """Constructed the way betfairlightweight does: message via the wrapped exception."""
    return APIError(None, method=None, params=None, exception=RuntimeError(msg))


def _status_error(msg: str) -> StatusCodeError:
    return StatusCodeError(msg)


class TestTransientClassification:
    @pytest.mark.parametrize(
        "msg",
        [
            "TIMEOUT_ERROR",
            "ANGX-0010",
            "HTTPSConnectionPool(host='identitysso-cert.betfair.com', port=443): "
            "Read timed out. (read timeout=16)",
            "('Connection aborted.', ConnectionResetError(54, 'Connection reset by peer'))",
        ],
    )
    def test_transient_api_errors(self, msg) -> None:
        assert is_transient_betfair_error(_api_error(msg))

    def test_genuine_api_error_is_not_transient(self) -> None:
        assert not is_transient_betfair_error(_api_error("INVALID_SESSION_INFORMATION"))

    @pytest.mark.parametrize("code", ["429", "502", "503", "504"])
    def test_rate_limit_status_codes_are_transient(self, code) -> None:
        assert is_transient_betfair_error(_status_error(f"Status code error: {code}"))

    def test_status_400_is_not_transient(self) -> None:
        assert not is_transient_betfair_error(_status_error("Status code error: 400"))

    def test_unrelated_exception_is_not_transient(self) -> None:
        assert not is_transient_betfair_error(ValueError("nope"))


class TestRetry:
    def test_succeeds_on_a_later_attempt(self) -> None:
        calls = {"n": 0}

        def flaky():
            calls["n"] += 1
            if calls["n"] < 3:
                raise _api_error("Read timed out.")
            return "logged in"

        assert (
            retry_betfair_call(flaky, max_attempts=3, sleep=lambda _s: None)
            == "logged in"
        )
        assert calls["n"] == 3

    def test_status_code_error_is_retried(self) -> None:
        """The defect this module exists for: 429 was invisible to the old retry."""
        calls = {"n": 0}

        def rate_limited():
            calls["n"] += 1
            if calls["n"] == 1:
                raise _status_error("Status code error: 429")
            return "ok"

        assert retry_betfair_call(rate_limited, sleep=lambda _s: None) == "ok"

    def test_non_transient_fails_fast(self) -> None:
        calls = {"n": 0}

        def rejected():
            calls["n"] += 1
            raise _api_error("INVALID_APP_KEY")

        with pytest.raises(APIError):
            retry_betfair_call(rejected, max_attempts=5, sleep=lambda _s: None)
        assert calls["n"] == 1, "a credential rejection must not be hammered"

    def test_final_attempt_raises_the_original(self) -> None:
        with pytest.raises(APIError, match="timed out"):
            retry_betfair_call(
                lambda: (_ for _ in ()).throw(_api_error("Read timed out.")),
                max_attempts=3,
                sleep=lambda _s: None,
            )

    def test_backoff_is_bounded(self) -> None:
        delays: list[float] = []

        def failing():
            raise _api_error("TIMEOUT_ERROR")

        with pytest.raises(APIError):
            retry_betfair_call(failing, max_attempts=6, sleep=delays.append)

        assert delays == [2.0, 4.0, 8.0, 16.0, 20.0], "capped at 20s, no storm"


def test_login_is_wrapped_in_the_retry() -> None:
    """auth.build_client must not call login() bare -- that was Monday 09:00."""
    import inspect

    from betfair_results_downloader.scheduler import auth

    src = inspect.getsource(auth)
    assert "retry_betfair_call(client.login" in src


class TestRawRequestsFailures:
    """
    The installed betfairlightweight wraps every exception into APIError
    (login.py and baseendpoint.py both end in a broad `except Exception`),
    so raw requests failures cannot currently escape. These exist as
    insurance against a future library version narrowing that wrapping.
    """

    def test_raw_read_timeout_is_transient_and_retried(self) -> None:
        import requests

        calls = {"n": 0}

        def flaky():
            calls["n"] += 1
            if calls["n"] == 1:
                raise requests.exceptions.ReadTimeout("Read timed out.")
            return "ok"

        assert retry_betfair_call(flaky, sleep=lambda _s: None) == "ok"

    def test_raw_connection_error_is_transient(self) -> None:
        import requests

        assert is_transient_betfair_error(
            requests.exceptions.ConnectionError("Connection aborted.")
        )

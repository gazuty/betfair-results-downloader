"""
Shared transient-failure policy for Betfair API calls.

One place decides what "transient" means, because the evidence says the old
per-site message matching was too narrow: on 2026-08-31 the 09:00 scheduled
run died on a single un-retried login (cert-SSO read timeout), and 2026-07-19
shows the same shape as a connection reset. Both were momentary; neither got
a second attempt.

Also load-bearing: betfairlightweight's StatusCodeError (HTTP 429/502/503)
subclasses BetfairError, NOT APIError, so an ``except APIError`` retry can
never see a rate-limit response however its message is matched.
"""

from __future__ import annotations

import time
from typing import Any, Callable, TypeVar

import requests
from betfairlightweight.exceptions import APIError, StatusCodeError

T = TypeVar("T")

# Betfair's own transient markers, plus the requests-level failures that
# betfairlightweight wraps into APIError text.
_TRANSIENT_API_MARKERS = (
    "TIMEOUT_ERROR",
    "ANGX-0010",
    "timed out",
    "Connection aborted",
    "Connection reset",
    "ConnectionError",
    "Max retries exceeded",
    "Temporary failure in name resolution",
)

_TRANSIENT_STATUS_MARKERS = ("429", "502", "503", "504")


def is_transient_betfair_error(exc: Exception) -> bool:
    """True when a retry has a realistic chance of succeeding."""
    # Raw requests failures. The installed betfairlightweight wraps every
    # exception into APIError (login.py and baseendpoint.py both end in a
    # broad `except Exception`), so today these cannot escape -- verified
    # against the live 09:00 incident, whose ReadTimeout arrived wrapped.
    # Classified anyway: three lines of insurance against a future library
    # version narrowing its wrapping, in the module whose whole job is
    # resilience.
    if isinstance(
        exc, (requests.exceptions.Timeout, requests.exceptions.ConnectionError)
    ):
        return True
    msg = str(exc)
    if isinstance(exc, StatusCodeError):
        return any(code in msg for code in _TRANSIENT_STATUS_MARKERS)
    if isinstance(exc, APIError):
        return any(marker in msg for marker in _TRANSIENT_API_MARKERS)
    return False


def retry_betfair_call(
    fn: Callable[[], T],
    *,
    max_attempts: int = 5,
    max_delay: float = 20.0,
    sleep: Callable[[float], Any] = time.sleep,
) -> T:
    """
    Call ``fn`` with exponential backoff on transient Betfair failures.

    Non-transient errors and the final attempt raise unchanged, so genuine
    API errors (bad filter, auth rejection) still fail fast.
    """
    for attempt in range(1, max_attempts + 1):
        try:
            return fn()
        except (
            APIError,
            StatusCodeError,
            requests.exceptions.Timeout,
            requests.exceptions.ConnectionError,
        ) as exc:
            if not is_transient_betfair_error(exc) or attempt == max_attempts:
                raise
            sleep(min(2.0**attempt, max_delay))
    raise AssertionError("unreachable")  # pragma: no cover

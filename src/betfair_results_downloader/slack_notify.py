"""
Post messages to Slack without an LLM agent in the path.

Config is read from ``~/.betfair/slack.json`` first, falling back to a
``slack`` section in credentials.json. Either shape::

    {"bot_token": "xoxb-...", "channel": "U0AMX1TM1M3", "enabled": true}

The local-file path is preferred deliberately: it lives on local disk rather
than in OneDrive, so a failure can still be announced even when
credentials.json is the file that could not be read.

``channel`` may be a channel ID/name or a user ID (a user ID opens a DM).
Uses only the standard library so the scheduled job gains no dependencies.
"""

from __future__ import annotations

import http.client
import json
import os
import time
import urllib.error
import urllib.request
from typing import Any, Callable, Optional, Tuple

_SLACK_POST_URL = "https://slack.com/api/chat.postMessage"
_TIMEOUT_SECONDS = 15
_MAX_ATTEMPTS = 3
_BACKOFF_CAP_SECONDS = 8.0
_RETRY_AFTER_CAP_SECONDS = 30.0


class SlackNotConfigured(RuntimeError):
    """Raised when credentials.json has no usable slack section."""


LOCAL_SLACK_CONFIG = os.path.expanduser("~/.betfair/slack.json")


def _read_local_config() -> Optional[dict[str, Any]]:
    try:
        with open(LOCAL_SLACK_CONFIG, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def load_slack_config(creds: Optional[dict[str, Any]] = None) -> Tuple[str, str]:
    """
    Return (bot_token, channel), preferring ~/.betfair/slack.json over the
    ``slack`` section of credentials.json. Raises SlackNotConfigured.

    The two sources are merged per field rather than chosen wholesale: a local
    file that exists but is incomplete (half-written, or holding only a
    channel) must not shadow a working embedded config and silence alerting
    altogether.
    """
    # creds may be any JSON shape: a syntactically valid credentials.json with
    # an array at the top level makes validation raise, and the failure path
    # then hands that value straight back here. Anything but a mapping is no
    # config at all -- and must not stop the local file announcing the problem.
    if not isinstance(creds, dict):
        creds = {}
    embedded = creds.get("slack") or {}
    if not isinstance(embedded, dict):
        embedded = {}
    local = _read_local_config() or {}
    cfg = {**embedded, **{k: v for k, v in local.items() if v not in (None, "")}}

    # `enabled` follows whichever source actually supplies the credentials.
    # Merging it like any other field would let a stale `enabled: false` in
    # credentials.json silence a complete local config that never mentions the
    # flag -- which, before the merge, was selected wholesale and worked.
    if "enabled" in local:
        enabled = bool(local["enabled"])
    elif str(local.get("bot_token", "")).strip():
        enabled = True
    else:
        enabled = bool(embedded.get("enabled", True))
    if not enabled:
        raise SlackNotConfigured("slack config is disabled (enabled=false)")
    token = str(cfg.get("bot_token", "")).strip()
    channel = str(cfg.get("channel", "")).strip()
    if not token:
        raise SlackNotConfigured(
            f"no slack bot_token in {LOCAL_SLACK_CONFIG} or credentials.json"
        )
    if not channel:
        raise SlackNotConfigured(
            f"no slack channel in {LOCAL_SLACK_CONFIG} or credentials.json"
        )
    return token, channel


def _parse_retry_after(value: Optional[str]) -> Optional[float]:
    """Slack's Retry-After is whole seconds; anything unparsable is ignored."""
    if not value:
        return None
    try:
        seconds = float(value)
    except ValueError:
        return None
    if seconds < 0:
        return None
    return min(seconds, _RETRY_AFTER_CAP_SECONDS)


def post_message(
    text: str,
    creds: Optional[dict[str, Any]] = None,
    channel_override: Optional[str] = None,
    *,
    sleep: Callable[[float], None] = time.sleep,
) -> str:
    """
    Post ``text`` to Slack. Returns the message timestamp on success.

    Transient failures -- network errors, timeouts, HTTP 429/5xx, and
    garbage response bodies -- are retried up to ``_MAX_ATTEMPTS`` times
    with exponential backoff, honouring Slack's Retry-After header on 429.
    This module carries the pipeline's failure alerts, so a single dropped
    packet must not silence the very message that reports a problem.

    Raises SlackNotConfigured if unconfigured (never retried), or
    RuntimeError if Slack rejects the call or every attempt fails.
    Callers decide whether that is fatal.
    """
    token, channel = load_slack_config(creds)
    payload = json.dumps(
        {
            "channel": channel_override or channel,
            "text": text,
            "unfurl_links": False,
            "unfurl_media": False,
        }
    ).encode("utf-8")

    req = urllib.request.Request(
        _SLACK_POST_URL,
        data=payload,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=utf-8",
        },
        method="POST",
    )

    last_error: Optional[Exception] = None
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        delay: Optional[float] = None
        try:
            with urllib.request.urlopen(req, timeout=_TIMEOUT_SECONDS) as resp:
                body = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            # HTTPError first: it subclasses URLError. 429 and 5xx are
            # transient; any other status is Slack telling us the request
            # itself is wrong, and repeating it would not change the answer.
            if exc.code == 429:
                delay = _parse_retry_after(exc.headers.get("Retry-After"))
            elif exc.code < 500:
                raise RuntimeError(f"Slack request failed: {exc}") from exc
            last_error = exc
        except (OSError, ValueError, http.client.HTTPException) as exc:
            # OSError covers URLError (DNS/connection failures), TimeoutError
            # (a read that times out after the connection is up), and
            # ConnectionResetError raised by resp.read() once urlopen has
            # already returned. http.client.HTTPException covers
            # IncompleteRead -- a peer that closes mid-body. ValueError
            # covers JSONDecodeError/UnicodeDecodeError on a garbled body.
            last_error = exc
        else:
            if not body.get("ok"):
                raise RuntimeError(f"Slack API error: {body.get('error', 'unknown')}")
            return str(body.get("ts", ""))
        if attempt < _MAX_ATTEMPTS:
            sleep(
                delay if delay is not None else min(2.0**attempt, _BACKOFF_CAP_SECONDS)
            )

    raise RuntimeError(
        f"Slack request failed after {_MAX_ATTEMPTS} attempts: {last_error}"
    ) from last_error

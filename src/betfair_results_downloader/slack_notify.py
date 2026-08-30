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

import json
import os
import urllib.error
import urllib.request
from typing import Any, Optional, Tuple

_SLACK_POST_URL = "https://slack.com/api/chat.postMessage"
_TIMEOUT_SECONDS = 15


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
    """
    cfg = _read_local_config()
    if cfg is None:
        cfg = (creds or {}).get("slack") or {}
    if not cfg.get("enabled", True):
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


def post_message(
    text: str,
    creds: Optional[dict[str, Any]] = None,
    channel_override: Optional[str] = None,
) -> str:
    """
    Post ``text`` to Slack. Returns the message timestamp on success.

    Raises SlackNotConfigured if unconfigured, or RuntimeError if Slack
    rejects the call. Callers decide whether that is fatal.
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
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT_SECONDS) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Slack request failed: {exc}") from exc

    if not body.get("ok"):
        raise RuntimeError(f"Slack API error: {body.get('error', 'unknown')}")
    return str(body.get("ts", ""))

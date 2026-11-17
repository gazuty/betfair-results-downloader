"""
Short forms of errors, for alerts and run messages.

A Betfair API error renders as the call, its parameters (every market id
asked about), the error body and then the full response again; an
``InvalidResponse`` carries the whole reply it could not parse. One
scheduled run on 2026-11-17 put about 20,000 characters of that into a
single Slack alert, burying the three lines that said what had failed.

The alert and ``run_history.jsonl`` get the short form from
:func:`brief_error`. The full text is not lost: every caller also logs the
exception itself, so it is in the scheduler's log.
"""

from __future__ import annotations

import re

# Long enough for an ordinary exception message, short enough to read in a
# phone notification.
DEFAULT_LIMIT = 200

_FULL_TEXT_NOTE = "full text in the log"

_RE_ERROR_CODE = re.compile(r"'errorCode':\s*'([^']+)'")
_RE_ERROR_MESSAGE = re.compile(r"'message':\s*'([^']+)'")
_RE_WHITESPACE = re.compile(r"\s+")


def brief_error(exc: BaseException, limit: int = DEFAULT_LIMIT) -> str:
    """
    ``exc`` as ``TypeName: what happened``, short enough for an alert.

    - A Betfair API error becomes the call and Betfair's error code:
      ``APIError: SportsAPING/v1.0/listMarketBook returned UNEXPECTED_ERROR
      (ANGX-0006)``.
    - An ``InvalidResponse`` says the reply could not be used and how large
      it was, not what it contained.
    - Anything else keeps its own message on one line, cut at ``limit``
      characters with a note that the rest is in the log.
    """
    name = type(exc).__name__
    text = str(exc)

    code = _RE_ERROR_CODE.search(text)
    if code:
        call = text.strip().splitlines()[0].strip() if text.strip() else "Betfair"
        message = _RE_ERROR_MESSAGE.search(text)
        detail = f" ({message.group(1)})" if message else ""
        return f"{name}: {_cut(call, limit)} returned {code.group(1)}{detail}"

    if name == "InvalidResponse" or text.lstrip().startswith("Invalid response"):
        return (
            f"{name}: Betfair's reply was missing required fields "
            f"({len(text):,} characters; {_FULL_TEXT_NOTE})"
        )

    one_line = _RE_WHITESPACE.sub(" ", text).strip()
    return f"{name}: {_cut(one_line, limit)}" if one_line else name


def _cut(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return f"{text[:limit].rstrip()}… (truncated; {_FULL_TEXT_NOTE})"

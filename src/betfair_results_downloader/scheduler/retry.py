"""
Hourly retries after a run that did not deliver.

The scheduler runs four times a day. When one of those runs fails, or
succeeds without refreshing the results feed, the next attempt used to be
the next scheduled run, up to ten hours away, while the dashboard kept
publishing the old feed. An hourly job (``retry``, installed with
``schedule install-retry`` on the hours that have no scheduled run) now
looks at the run history and runs the pipeline again when the last attempt
asked for it.

It stops after :data:`MAX_FAILURES_IN_A_ROW` consecutive failures, so a
long outage costs six alerts, not one an hour for as long as it lasts. The
scheduled runs still happen, and the first success of any run clears the
count and re-arms the retries.

The state is the run history itself: each scheduled or retry attempt
records ``retry_wanted``, and the count is the number of trailing records
that asked. There is no separate state file to fall out of step with it.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

MAX_FAILURES_IN_A_ROW = 6

HISTORY_FILENAME = "run_history.jsonl"


@dataclass(frozen=True)
class RetryDecision:
    run: bool
    failures: int
    reason: str


def _attempts_newest_first(log_dir: str | Path) -> list[dict]:
    """
    Scheduled and retry attempts in the history, newest first.

    Manual backfills are left out: they neither repair a failed scheduled
    run nor count against it. So are lines that are not records (a torn
    line from a crash mid-append). An unreadable history is no evidence of
    a failure: it reads as empty and the reason is logged.
    """
    path = Path(log_dir) / HISTORY_FILENAME
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    except OSError as exc:
        logger.warning("Could not read %s (%s); no retry is pending.", path, exc)
        return []

    attempts: list[dict] = []
    for line in reversed(lines):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if not isinstance(record, dict):
            continue
        # A backfill is marked "mode" when it ran and "kind" when it was
        # refused for disk space; both are manual and both are ignored.
        if "backfill" in (record.get("mode"), record.get("kind")):
            continue
        attempts.append(record)
    return attempts


def _asked_in_a_row(attempts: list[dict]) -> int:
    count = 0
    for record in attempts:
        # A record written before retry_wanted existed did not ask, so
        # history from before this feature reads as a clean slate.
        if record.get("retry_wanted") is not True:
            break
        count += 1
    return count


def failures_in_a_row(log_dir: str | Path) -> int:
    """
    How many of the most recent scheduled or retry attempts asked for a
    retry, counting back from the newest until one that did not.
    """
    return _asked_in_a_row(_attempts_newest_first(log_dir))


def failures_before_latest(log_dir: str | Path) -> int:
    """
    The same count as it stood before the newest attempt was recorded.

    A run reads this after it has recorded itself, to say how many failures
    it recovered from. Reading the count before the run would be wrong when
    two runs overlap (after a wake launchd can start a scheduled run and a
    retry together): the second would announce a recovery the first had
    already made.
    """
    return _asked_in_a_row(_attempts_newest_first(log_dir)[1:])


def decide_retry(log_dir: str | Path) -> RetryDecision:
    """Whether the hourly job should run the pipeline now, and why."""
    failures = failures_in_a_row(log_dir)
    if failures == 0:
        return RetryDecision(False, 0, "No retry pending: the last run delivered.")
    if failures >= MAX_FAILURES_IN_A_ROW:
        return RetryDecision(
            False,
            failures,
            f"Not retrying: {failures} failures in a row (the limit is "
            f"{MAX_FAILURES_IN_A_ROW}). The next attempt is the next scheduled run.",
        )
    return RetryDecision(
        True,
        failures,
        f"Retrying: the last {failures} attempt(s) did not deliver.",
    )

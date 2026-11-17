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
The price is that the limit depends on that file being writable. So a
retry claims its attempt first, by writing a "started" record to disk
under the pipeline lock, and does not run if that write fails (see
:func:`claim_attempt`): every retry that runs has already been counted.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

MAX_FAILURES_IN_A_ROW = 6

HISTORY_FILENAME = "run_history.jsonl"

# How attempts are marked in the history: "kind" says who made the attempt,
# and a retry's claim carries the "started" phase until its outcome follows.
KIND_SCHEDULED = "scheduled"
KIND_RETRY = "retry"
PHASE_STARTED = "started"


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
    return _without_completed_claims(attempts)


def _without_completed_claims(newest_first: list[dict]) -> list[dict]:
    """
    Drop each retry's "started" claim once its outcome has been recorded
    straight after it, so one retry is one attempt. A claim with no outcome
    (the retry crashed, or its final record could not be written) stays,
    and counts as the failure it stands for.

    Worked through in the order the records were written, so that one
    outcome settles exactly one claim: a dangling claim followed by a later
    retry's claim and outcome is still one failure of its own.
    """
    kept: list[dict] = []
    for record in reversed(newest_first):
        is_outcome = (
            record.get("kind") == KIND_RETRY and record.get("phase") != PHASE_STARTED
        )
        if is_outcome and kept and kept[-1].get("phase") == PHASE_STARTED:
            kept.pop()
        kept.append(record)
    kept.reverse()
    return kept


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


def claim_attempt(log_dir: str | Path) -> RetryDecision:
    """
    Decide again, and if a retry is still due, record that it has started.

    Called by the retry while it holds the pipeline lock, for two reasons.

    - Another attempt may have delivered since the retry first looked: a
      scheduled run started at the same moment (launchd starts both after a
      wake) records its result before it releases the lock, so a retry that
      then gets the lock sees it here and stands down.
    - The six-in-a-row limit is counted from the history. The claim is a
      real line flushed to disk, written before the pipeline runs. If it
      cannot be written the retry does not run, so a full or read-only log
      directory cannot leave a pending record driving the pipeline every
      hour with the count never moving.

    The claim itself counts as a failed attempt until the retry's outcome
    is recorded after it.
    """
    from .state import append_run_history  # noqa: PLC0415

    decision = decide_retry(log_dir)
    if not decision.run:
        return decision
    claimed = append_run_history(
        log_dir,
        {
            "kind": KIND_RETRY,
            "phase": PHASE_STARTED,
            "status": "started",
            "retry_wanted": True,
            "message": "Retry started.",
        },
        durable=True,
    )
    if not claimed:
        return RetryDecision(
            False,
            decision.failures,
            f"Not retrying: {HISTORY_FILENAME} in {log_dir} cannot be written, so "
            f"this attempt could not be counted against the limit of "
            f"{MAX_FAILURES_IN_A_ROW}. The next attempt is the next scheduled run.",
        )
    return decision

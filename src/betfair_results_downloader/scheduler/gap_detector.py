"""
scheduler/gap_detector.py
~~~~~~~~~~~~~~~~~~~~~~~~~
Determines the date window that a scheduled run should download (Phase 2.2).

``compute_backfill_window`` resolves the ``from_date``/``to_date`` pair using
a three-level cascade:

1. **Azure ScheduleState** — ``dbo.ScheduleState.LastCoveredDateLocal`` for
   this user when present, otherwise ``LastCoveredDateUtc``, read via
   :func:`state.read_schedule_state`.
2. **Canonical CSV** — the maximum ``settledDate`` in
   ``cleared_orders_cleaned.csv``, read directly from the resolved results
   directory.
3. **Cold-start fallback** — ``max_backfill_days`` days before today.

In all cases the window is capped at ``schedule_cfg.max_backfill_days`` days
from the scheduler-local day. The ``min_coverage_overlap_days`` setting causes
the from-date to be pulled back slightly to re-pull a safety overlap of
already-covered data.

IMPORTANT: This module must NEVER import or call recommend_lookback_days()
or read run_state.json. The scheduler has its own state cascade
(Azure ScheduleState → canonical CSV → cold-start) that is independent
of the GUI's state system.
"""
from __future__ import annotations

import logging
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Optional, Tuple

import pandas as pd

from ..config import ScheduleConfig
from ..paths import resolve_results_dir
from .state import read_schedule_state
from .time_semantics import get_scheduler_now

logger = logging.getLogger(__name__)

# Return type: (from_date, to_date, reason)
WindowResult = Tuple[date, date, str]


def _today_utc(schedule_cfg: ScheduleConfig) -> date:
    return get_scheduler_now(schedule_cfg).today_utc


def _today_local(schedule_cfg: ScheduleConfig) -> date:
    return get_scheduler_now(schedule_cfg).today_local


def _max_settled_date_from_csv(results_dir: Path) -> Optional[date]:
    """
    Return the latest ``settledDate`` from the canonical CSV, or ``None`` if
    the file is missing, empty, or unparseable.

    Reads exactly one column from one file — no hidden dependencies.
    """
    csv_path = results_dir / "cleared_orders_cleaned.csv"
    if not csv_path.exists():
        return None
    try:
        df = pd.read_csv(csv_path, usecols=["settledDate"], low_memory=False)
        if df.empty:
            return None
        ts = pd.to_datetime(df["settledDate"], utc=True, errors="coerce").dropna()
        if ts.empty:
            return None
        return ts.max().date()
    except Exception as exc:
        logger.debug("CSV max-date lookup failed for %s: %s", csv_path, exc)
        return None


def compute_backfill_window(
    creds: dict[str, Any],
    schedule_cfg: ScheduleConfig,
) -> WindowResult:
    """
    Compute the ``(from_date, to_date)`` window that the scheduler should
    download on the current run.

    Resolution order:

    1. ``dbo.ScheduleState.LastCoveredDateLocal`` (Azure) — authoritative when
       reachable, with ``LastCoveredDateUtc`` as a fallback for backward compatibility.
    2. Canonical CSV max ``settledDate`` — fallback when Azure is unavailable.
    3. ``today - max_backfill_days`` — cold-start fallback.

    The *from_date* is set to ``last_covered + 1`` (or ``today - fallback``
    for the cold-start case), then pulled back by
    ``min_coverage_overlap_days`` to re-pull a safety overlap. The result is
    then capped at ``max_backfill_days`` days before the scheduler-local ``today``.

    ``to_date`` is the scheduler-local current date.
    """
    scheduler_now = get_scheduler_now(schedule_cfg)
    today_local = scheduler_now.today_local
    to_date = today_local
    earliest_allowed = today_local - timedelta(days=schedule_cfg.max_backfill_days)

    azure_state = read_schedule_state(creds)
    if azure_state is not None:
        last_covered = azure_state.last_covered_date_local
        last_covered_source = "LastCoveredDateLocal"
        if last_covered is None and azure_state.last_covered_date_utc is not None:
            last_covered = azure_state.last_covered_date_utc
            last_covered_source = "LastCoveredDateUtc"

        if last_covered is not None:
            base_from = last_covered - timedelta(days=schedule_cfg.min_coverage_overlap_days) + timedelta(days=1)
            from_date = max(base_from, earliest_allowed)
            reason = (
                f"Azure ScheduleState ({last_covered_source}): last_covered={last_covered}, "
                f"overlap={schedule_cfg.min_coverage_overlap_days}d → from={from_date}"
            )
            if base_from < earliest_allowed:
                reason += f" (capped at max_backfill_days={schedule_cfg.max_backfill_days})"
            logger.info("Gap detection via Azure: %s", reason)
            return from_date, to_date, reason

    results_dir = resolve_results_dir(creds)
    csv_last = _max_settled_date_from_csv(results_dir)
    if csv_last is not None:
        base_from = csv_last - timedelta(days=schedule_cfg.min_coverage_overlap_days) + timedelta(days=1)
        from_date = max(base_from, earliest_allowed)
        reason = (
            f"CSV max settledDate: {csv_last}, "
            f"overlap={schedule_cfg.min_coverage_overlap_days}d → from={from_date}"
        )
        if base_from < earliest_allowed:
            reason += f" (capped at max_backfill_days={schedule_cfg.max_backfill_days})"
        logger.info("Gap detection via CSV: %s", reason)
        return from_date, to_date, reason

    logger.warning(
        "CSV fallback failed: resolved results_csv_dir=%s but no valid "
        "settledDate found. Falling through to %d-day cold-start.",
        results_dir, schedule_cfg.max_backfill_days,
    )
    from_date = earliest_allowed
    reason = (
        f"Cold-start: no Azure state or CSV data found — "
        f"backfilling {schedule_cfg.max_backfill_days} days from today"
    )
    logger.info("Gap detection cold-start: %s", reason)
    return from_date, to_date, reason

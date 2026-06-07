"""
scheduler/gap_detector.py
~~~~~~~~~~~~~~~~~~~~~~~~~
Determines the timestamp window that a scheduled run should download.

Resolution order for the incremental checkpoint:

1. **Azure ScheduleState** — ``LastConfirmedSettledAtUtc`` when present.
2. **Canonical CSV** — maximum ``settledDate`` from ``cleared_orders_cleaned.csv``.
3. **Cold-start fallback** — ``max_backfill_days`` before scheduler-now.

The checkpoint is always pulled back by ``min_overlap_hours`` for a safety
re-download window, then capped by ``max_backfill_days``.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional, Tuple

import pandas as pd

from ..config import ScheduleConfig
from ..paths import resolve_results_dir
from .state import read_schedule_state
from .time_semantics import get_scheduler_now

logger = logging.getLogger(__name__)

WindowResult = Tuple[datetime, datetime, str]


def _max_settled_datetime_from_csv(results_dir: Path) -> Optional[datetime]:
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
        latest = ts.max().to_pydatetime()
        if latest.tzinfo is None:
            latest = latest.replace(tzinfo=timezone.utc)
        return latest.astimezone(timezone.utc)
    except Exception as exc:
        logger.debug("CSV max-datetime lookup failed for %s: %s", csv_path, exc)
        return None


def _max_settled_date_from_csv(results_dir: Path) -> Optional[date]:
    latest = _max_settled_datetime_from_csv(results_dir)
    return latest.date() if latest is not None else None


def compute_backfill_window(
    creds: dict[str, Any],
    schedule_cfg: ScheduleConfig,
) -> WindowResult:
    scheduler_now = get_scheduler_now(schedule_cfg)
    now_utc = scheduler_now.now_utc
    earliest_allowed = now_utc - timedelta(days=schedule_cfg.max_backfill_days)

    azure_state = read_schedule_state(creds)
    if azure_state is not None:
        checkpoint: Optional[datetime] = None
        checkpoint_source: Optional[str] = None

        if azure_state.last_confirmed_settled_at_utc is not None:
            checkpoint = azure_state.last_confirmed_settled_at_utc.astimezone(timezone.utc)
            checkpoint_source = "LastConfirmedSettledAtUtc"
        elif azure_state.last_covered_date_utc is not None:
            checkpoint = datetime.combine(
                azure_state.last_covered_date_utc,
                datetime.min.time(),
                tzinfo=timezone.utc,
            )
            checkpoint_source = "LastCoveredDateUtc (legacy bootstrap)"
        elif azure_state.last_covered_date_local is not None:
            checkpoint = datetime.combine(
                azure_state.last_covered_date_local,
                datetime.min.time(),
                tzinfo=timezone.utc,
            )
            checkpoint = min(checkpoint, now_utc)
            checkpoint_source = "LastCoveredDateLocal (legacy bootstrap, capped)"

        if checkpoint is not None and checkpoint_source is not None:
            base_from = checkpoint - timedelta(hours=schedule_cfg.min_overlap_hours)
            from_dt = max(base_from, earliest_allowed)
            reason = (
                f"Azure ScheduleState ({checkpoint_source}): "
                f"checkpoint={checkpoint.isoformat()}, overlap={schedule_cfg.min_overlap_hours}h "
                f"→ from={from_dt.isoformat()}"
            )
            if base_from < earliest_allowed:
                reason += f" (capped at max_backfill_days={schedule_cfg.max_backfill_days})"
            logger.info("Gap detection via Azure checkpoint: %s", reason)
            return from_dt, now_utc, reason

    results_dir = resolve_results_dir(creds)
    csv_last = _max_settled_datetime_from_csv(results_dir)
    if csv_last is not None:
        base_from = csv_last - timedelta(hours=schedule_cfg.min_overlap_hours)
        from_dt = max(base_from, earliest_allowed)
        reason = (
            f"CSV max settledDate: {csv_last.isoformat()}, "
            f"overlap={schedule_cfg.min_overlap_hours}h → from={from_dt.isoformat()}"
        )
        if base_from < earliest_allowed:
            reason += f" (capped at max_backfill_days={schedule_cfg.max_backfill_days})"
        logger.info("Gap detection via CSV checkpoint: %s", reason)
        return from_dt, now_utc, reason

    logger.warning(
        "CSV fallback failed: resolved results_csv_dir=%s but no valid settledDate found. "
        "Falling through to %d-day cold-start.",
        results_dir,
        schedule_cfg.max_backfill_days,
    )
    from_dt = earliest_allowed
    reason = (
        f"Cold-start: no Azure state or CSV data found, backfilling "
        f"{schedule_cfg.max_backfill_days} days from now"
    )
    logger.info("Gap detection cold-start: %s", reason)
    return from_dt, now_utc, reason


def derive_coverage_dates(
    from_dt_utc: datetime,
    to_dt_utc: datetime,
    timezone_name: str,
) -> tuple[date, date]:
    from zoneinfo import ZoneInfo

    tz = ZoneInfo(timezone_name)
    covered_utc = to_dt_utc.astimezone(timezone.utc).date()
    covered_local = to_dt_utc.astimezone(tz).date()
    return covered_utc, covered_local


def cold_start_default_dates(schedule_cfg: ScheduleConfig) -> tuple[date, date]:
    scheduler_now = get_scheduler_now(schedule_cfg)
    start = (scheduler_now.now_utc - timedelta(days=schedule_cfg.max_backfill_days)).date()
    end = scheduler_now.now_utc.date()
    return start, end

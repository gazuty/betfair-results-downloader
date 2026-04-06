"""
scheduler/gap_detector.py
~~~~~~~~~~~~~~~~~~~~~~~~~
Determines the date window that a scheduled run should download (Phase 2.2).

``compute_backfill_window`` resolves the ``from_date``/``to_date`` pair using
a three-level cascade:

1. **Azure ScheduleState** — ``dbo.ScheduleState.LastCoveredDateUtc`` for
   this user, read via :func:`state.read_schedule_state`.
2. **Canonical CSV** — the maximum ``settledDate`` in the canonical CSV
   located at ``credentials["paths"]["results_csv_dir"]``, read via
   :func:`recommend.recommend_lookback_days`.
3. **Cold-start fallback** — ``max_backfill_days`` days before today.

In all cases the window is capped at ``schedule_cfg.max_backfill_days`` days
from today.  The ``min_coverage_overlap_days`` setting causes the from-date to
be pulled back slightly to re-pull a safety overlap of already-covered data.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional, Tuple

from ..config import ScheduleConfig
from .state import read_schedule_state

logger = logging.getLogger(__name__)

# Return type: (from_date, to_date, reason)
WindowResult = Tuple[date, date, str]


def _today_utc() -> date:
    return datetime.now(timezone.utc).date()


def _max_from_csv(creds: dict[str, Any]) -> Optional[date]:
    """
    Return the latest ``settledDate`` from the canonical CSV, or ``None`` on
    any failure (missing config, missing file, parse error, empty column).
    """
    try:
        from ..recommend import recommend_lookback_days  # noqa: PLC0415
        results_dir_raw = (creds.get("paths") or {}).get("results_csv_dir", "")
        if not results_dir_raw:
            return None
        _, _, last_settled = recommend_lookback_days(Path(results_dir_raw))
        return last_settled
    except Exception as exc:
        logger.debug("CSV max-date lookup failed: %s", exc)
        return None


def compute_backfill_window(
    creds: dict[str, Any],
    schedule_cfg: ScheduleConfig,
) -> WindowResult:
    """
    Compute the ``(from_date, to_date)`` window that the scheduler should
    download on the current run.

    Resolution order:

    1. ``dbo.ScheduleState.LastCoveredDateUtc`` (Azure) — authoritative when
       reachable.
    2. Canonical CSV max ``settledDate`` — fallback when Azure is unavailable.
    3. ``today - max_backfill_days`` — cold-start fallback.

    The *from_date* is set to ``last_covered + 1`` (or ``today - fallback``
    for the cold-start case), then pulled back by
    ``min_coverage_overlap_days`` to re-pull a safety overlap.  The result is
    then capped at ``max_backfill_days`` days before ``today``.

    ``to_date`` is always today (UTC).

    Parameters
    ----------
    creds:
        Full credentials dict.
    schedule_cfg:
        Parsed :class:`ScheduleConfig`.

    Returns
    -------
    tuple[date, date, str]
        ``(from_date, to_date, reason_string)`` where ``reason_string``
        explains which detection path was used.
    """
    today = _today_utc()
    to_date = today

    # Oldest allowed from_date given max_backfill_days
    earliest_allowed = today - timedelta(days=schedule_cfg.max_backfill_days)

    # --- Path 1: Azure ScheduleState ---
    azure_state = read_schedule_state(creds)
    if azure_state is not None and azure_state.last_covered_date_utc is not None:
        last_covered = azure_state.last_covered_date_utc
        base_from = last_covered - timedelta(days=schedule_cfg.min_coverage_overlap_days) + timedelta(days=1)
        from_date = max(base_from, earliest_allowed)
        reason = (
            f"Azure ScheduleState: last_covered={last_covered}, "
            f"overlap={schedule_cfg.min_coverage_overlap_days}d → from={from_date}"
        )
        if base_from < earliest_allowed:
            reason += f" (capped at max_backfill_days={schedule_cfg.max_backfill_days})"
        logger.info("Gap detection via Azure: %s", reason)
        return from_date, to_date, reason

    # --- Path 2: Canonical CSV ---
    csv_last = _max_from_csv(creds)
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

    # --- Path 3: Cold-start fallback ---
    from_date = earliest_allowed
    reason = (
        f"Cold-start: no Azure state or CSV data found — "
        f"backfilling {schedule_cfg.max_backfill_days} days from today"
    )
    logger.info("Gap detection cold-start: %s", reason)
    return from_date, to_date, reason

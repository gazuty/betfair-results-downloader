from __future__ import annotations

import csv
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any


def _parse_iso_to_utc(value: str | None) -> datetime | None:
    if not value:
        return None
    raw = value.strip()
    if not raw:
        return None
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(raw)
    except Exception:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def compute_missing_settled_dates(
    csv_path: Path,
    *,
    max_ranges: int = 10,
    window_days: int | None = 90,
    now_utc: datetime | None = None,
) -> dict[str, Any]:
    if not csv_path.exists():
        return {
            "window_start": None,
            "window_end": None,
            "earliest": None,
            "latest": None,
            "today": (now_utc or datetime.now(timezone.utc)).date().isoformat(),
            "num_missing": 0,
            "missing_ranges": [],
            "message": "Canonical CSV not found.",
        }

    seen_dates: set[date] = set()
    earliest: date | None = None
    latest: date | None = None
    latest_dt: datetime | None = None

    with csv_path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            settled_dt = _parse_iso_to_utc(row.get("settledDate"))
            if settled_dt is None:
                continue
            if latest_dt is None or settled_dt > latest_dt:
                latest_dt = settled_dt
            settled = settled_dt.date()
            seen_dates.add(settled)
            if earliest is None or settled < earliest:
                earliest = settled
            if latest is None or settled > latest:
                latest = settled

    now = now_utc or datetime.now(timezone.utc)
    today_utc = now.date()

    def _staleness() -> tuple[float | None, int | None]:
        """
        Staleness from the settlement timestamp, not the date boundary.

        A row settled at 23:59 is minutes old at 00:01 the next day; measuring
        by date would call that a full day stale and raise a false alarm on
        every run just after UTC midnight.
        """
        if latest_dt is None:
            return None, None
        hours = max((now - latest_dt).total_seconds() / 3600.0, 0.0)
        return round(hours, 2), int(hours // 24)

    if earliest is None or latest is None:
        return {
            "window_start": None,
            "window_end": None,
            "earliest": None,
            "latest": None,
            "today": today_utc.isoformat(),
            "hours_stale": None,
            "days_stale": None,
            "num_missing": 0,
            "missing_ranges": [],
            "message": "No settledDate values found.",
        }

    window_start = (
        today_utc - timedelta(days=int(window_days))
        if window_days is not None
        else earliest
    )
    window_end = today_utc

    present_in_window = sorted(d for d in seen_dates if window_start <= d <= today_utc)
    if not present_in_window:
        return {
            "window_start": window_start.isoformat(),
            "window_end": window_end.isoformat(),
            "audit_start": None,
            "audit_end": None,
            "earliest": earliest.isoformat(),
            "latest": latest.isoformat(),
            "today": today_utc.isoformat(),
            "hours_stale": _staleness()[0],
            "days_stale": _staleness()[1],
            "num_missing": 0,
            "missing_ranges": [],
        }

    audit_start = present_in_window[0]
    # Deliberately not present_in_window[-1]: clamping to the last day that
    # HAS data makes a stopped pipeline structurally invisible, because the
    # days between the final row and today are never examined.
    #
    # But stop at the last COMPLETED day. Today is still in progress, so
    # counting it would report a missing day on every run made before the
    # first settlement of the day.
    audit_end = max(today_utc - timedelta(days=1), audit_start)

    present_set = set(present_in_window)
    missing_ranges: list[dict[str, Any]] = []
    num_missing = 0
    cur = audit_start
    range_start: date | None = None

    while cur <= audit_end:
        if cur not in present_set:
            num_missing += 1
            if range_start is None:
                range_start = cur
        else:
            if range_start is not None:
                missing_ranges.append(
                    {
                        "start": range_start.isoformat(),
                        "end": (cur - timedelta(days=1)).isoformat(),
                        "days": (cur - range_start).days,
                    }
                )
                range_start = None
        cur += timedelta(days=1)

    if range_start is not None:
        missing_ranges.append(
            {
                "start": range_start.isoformat(),
                "end": audit_end.isoformat(),
                "days": (audit_end - range_start).days + 1,
            }
        )

    if max_ranges >= 0:
        missing_ranges = missing_ranges[:max_ranges]

    return {
        "window_start": window_start.isoformat(),
        "window_end": window_end.isoformat(),
        "audit_start": audit_start.isoformat(),
        "audit_end": audit_end.isoformat(),
        "earliest": earliest.isoformat(),
        "latest": latest.isoformat(),
        "today": today_utc.isoformat(),
        "hours_stale": _staleness()[0],
        "days_stale": _staleness()[1],
        "num_missing": num_missing,
        "missing_ranges": missing_ranges,
    }

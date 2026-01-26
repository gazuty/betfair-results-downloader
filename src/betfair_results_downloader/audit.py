from __future__ import annotations

import csv
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any


def _parse_iso_to_utc_date(value: str | None) -> date | None:
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
    return dt.astimezone(timezone.utc).date()


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

    with csv_path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            settled = _parse_iso_to_utc_date(row.get("settledDate"))
            if settled is None:
                continue
            seen_dates.add(settled)
            if earliest is None or settled < earliest:
                earliest = settled
            if latest is None or settled > latest:
                latest = settled

    today_utc = (now_utc or datetime.now(timezone.utc)).date()
    if earliest is None or latest is None:
        return {
            "window_start": None,
            "window_end": None,
            "earliest": None,
            "latest": None,
            "today": today_utc.isoformat(),
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
            "num_missing": 0,
            "missing_ranges": [],
        }

    audit_start = present_in_window[0]
    audit_end = present_in_window[-1]

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
        "num_missing": num_missing,
        "missing_ranges": missing_ranges,
    }

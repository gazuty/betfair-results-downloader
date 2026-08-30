"""
A stopped pipeline must be visible.

The audit previously clamped its scan to the last day that HAS data, so days
between the final row and today were never examined and a dead job reported
"no gaps found". The report has the mirror problem: it renders a confident
$0.00 from a stale file, which reads exactly like a quiet day.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from betfair_results_downloader.audit import compute_missing_settled_dates
from betfair_results_downloader.reporting.daily_dm_report import (
    STALE_AFTER_HOURS,
    build_daily_dm_report_from_dataframe,
)

SYDNEY = ZoneInfo("Australia/Sydney")


def _canonical(tmp_path, dates: list[str]):
    path = tmp_path / "cleared_orders_cleaned.csv"
    rows = "\n".join(
        f"{i},7,1.0,{d}T02:00:00.000Z" for i, d in enumerate(dates, start=1)
    )
    path.write_text(f"betId,eventTypeId,profit,settledDate\n{rows}\n", encoding="utf-8")
    return path


def test_audit_sees_days_after_the_last_row(tmp_path) -> None:
    """The failure mode: data stops, and the audit reports all clear."""
    path = _canonical(tmp_path, ["2026-01-10", "2026-01-11", "2026-01-12"])
    now = datetime(2026, 1, 17, 12, 0, tzinfo=timezone.utc)

    result = compute_missing_settled_dates(path, now_utc=now)

    assert result["days_stale"] == 5
    # 13th-16th: the completed days with no data. The 17th is today and still
    # in progress, so it is deliberately not counted as a gap -- staleness,
    # measured from the settlement timestamp, is what reports it.
    assert result["num_missing"] == 4
    assert result["missing_ranges"] == [
        {"start": "2026-01-13", "end": "2026-01-16", "days": 4}
    ]


def test_audit_is_quiet_when_data_is_current(tmp_path) -> None:
    now = datetime(2026, 1, 12, 12, 0, tzinfo=timezone.utc)
    path = _canonical(tmp_path, ["2026-01-10", "2026-01-11", "2026-01-12"])

    result = compute_missing_settled_dates(path, now_utc=now)

    assert result["days_stale"] == 0
    assert result["num_missing"] == 0
    assert result["missing_ranges"] == []


def test_interior_gaps_are_still_detected(tmp_path) -> None:
    now = datetime(2026, 1, 14, 12, 0, tzinfo=timezone.utc)
    path = _canonical(tmp_path, ["2026-01-10", "2026-01-13", "2026-01-14"])

    result = compute_missing_settled_dates(path, now_utc=now)

    assert result["num_missing"] == 2
    assert result["days_stale"] == 0


def _frame(settled_at_utc: datetime) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "betId": [1],
            "eventTypeId": [7],
            "profit": [10.0],
            "settledDate": [settled_at_utc.strftime("%Y-%m-%dT%H:%M:%S.000Z")],
        }
    )


def test_report_warns_when_data_is_stale() -> None:
    report_dt = datetime(2026, 6, 10, 19, 35, tzinfo=SYDNEY)
    df = _frame(report_dt.astimezone(timezone.utc) - timedelta(hours=30))

    report = build_daily_dm_report_from_dataframe(df, report_dt=report_dt)

    assert report.hours_stale is not None
    assert report.hours_stale == pytest.approx(30, abs=0.5)
    assert "Data may be stale" in report.text
    assert "30h" in report.text


def test_report_is_clean_when_data_is_fresh() -> None:
    report_dt = datetime(2026, 6, 10, 19, 35, tzinfo=SYDNEY)
    df = _frame(report_dt.astimezone(timezone.utc) - timedelta(hours=2))

    report = build_daily_dm_report_from_dataframe(df, report_dt=report_dt)

    assert report.hours_stale == pytest.approx(2, abs=0.5)
    assert "stale" not in report.text.lower()


def test_staleness_threshold_boundary() -> None:
    report_dt = datetime(2026, 6, 10, 19, 35, tzinfo=SYDNEY)
    just_under = _frame(
        report_dt.astimezone(timezone.utc) - timedelta(hours=STALE_AFTER_HOURS - 1)
    )
    just_over = _frame(
        report_dt.astimezone(timezone.utc) - timedelta(hours=STALE_AFTER_HOURS + 1)
    )

    assert (
        "stale"
        not in build_daily_dm_report_from_dataframe(
            just_under, report_dt=report_dt
        ).text.lower()
    )
    assert (
        "stale"
        in build_daily_dm_report_from_dataframe(
            just_over, report_dt=report_dt
        ).text.lower()
    )


def test_multi_day_staleness_reads_in_days() -> None:
    report_dt = datetime(2026, 6, 10, 19, 35, tzinfo=SYDNEY)
    df = _frame(report_dt.astimezone(timezone.utc) - timedelta(days=5))

    text = build_daily_dm_report_from_dataframe(df, report_dt=report_dt).text

    assert "5 days old" in text


def test_just_after_utc_midnight_is_not_stale(tmp_path) -> None:
    """
    A row settled at 23:59 is minutes old at 00:01. Measuring staleness by
    date boundary would raise a false alarm on every run made just after UTC
    midnight, before the first settlement of the new day.
    """
    path = tmp_path / "cleared_orders_cleaned.csv"
    path.write_text(
        "betId,eventTypeId,profit,settledDate\n1,7,1.0,2026-01-11T23:59:00.000Z\n",
        encoding="utf-8",
    )
    now = datetime(2026, 1, 12, 0, 1, tzinfo=timezone.utc)

    result = compute_missing_settled_dates(path, now_utc=now)

    assert result["days_stale"] == 0
    assert result["hours_stale"] < 1
    assert result["num_missing"] == 0, "today is still in progress, not a gap"


def test_today_in_progress_is_never_counted_as_a_gap(tmp_path) -> None:
    path = _canonical(tmp_path, ["2026-01-10", "2026-01-11"])
    now = datetime(2026, 1, 12, 6, 0, tzinfo=timezone.utc)

    result = compute_missing_settled_dates(path, now_utc=now)

    assert result["num_missing"] == 0
    assert result["audit_end"] == "2026-01-11", "scan stops at the last completed day"


def test_report_freshness_ignores_the_sport_filter() -> None:
    """
    A quiet day for horses and greyhounds while other event types settle
    normally is not a stalled pipeline, and must not be reported as one.
    """
    report_dt = datetime(2026, 6, 10, 19, 35, tzinfo=SYDNEY)
    utc = report_dt.astimezone(timezone.utc)
    df = pd.DataFrame(
        {
            "betId": [1, 2],
            # 7 = horses (old), 1 = soccer (current, excluded from totals)
            "eventTypeId": [7, 1],
            "profit": [10.0, 5.0],
            "settledDate": [
                (utc - timedelta(hours=30)).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                (utc - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
            ],
        }
    )

    report = build_daily_dm_report_from_dataframe(df, report_dt=report_dt)

    assert report.hours_stale == pytest.approx(1, abs=0.5)
    assert "stale" not in report.text.lower()

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd

from betfair_results_downloader.reporting.daily_dm_report import (
    build_daily_dm_report_from_dataframe,
)

SYDNEY_TZ = ZoneInfo("Australia/Sydney")


def test_daily_dm_report_uses_most_recent_sunday_and_current_day_windows() -> None:
    df = pd.DataFrame(
        [
            {
                "betId": "1",
                "eventTypeId": 7,
                "profit": 100.0,
                "settledDate": "2026-06-06T00:30:00Z",  # Sat 10:30 local
            },
            {
                "betId": "2",
                "eventTypeId": 4339,
                "profit": -20.0,
                "settledDate": "2026-06-03T10:00:00Z",  # Wed local, this week only
            },
            {
                "betId": "3",
                "eventTypeId": 7,
                "profit": 50.0,
                "settledDate": "2026-05-30T12:00:00Z",  # prior week, excluded
            },
            {
                "betId": "4",
                "eventTypeId": 1,
                "profit": 999.0,
                "settledDate": "2026-06-06T01:00:00Z",  # Sat 11:00 local, soccer
            },
        ]
    )

    report = build_daily_dm_report_from_dataframe(
        df,
        report_dt=datetime(2026, 6, 6, 21, 0, tzinfo=SYDNEY_TZ),
    )

    assert report.week_to_date.total_profit == 1079.0
    assert report.week_to_date.horses_profit == 100.0
    assert report.week_to_date.greyhounds_profit == -20.0

    assert report.day_to_date.total_profit == 1099.0
    assert report.day_to_date.horses_profit == 100.0
    assert report.day_to_date.greyhounds_profit == 0.0

    assert "Betfair results update" in report.text
    assert "Friday 6 June, 9:00 PM" not in report.text
    assert "Saturday 6 June, 9:00 PM" in report.text
    assert "Week to date (since Sunday 12:00 AM)" in report.text
    assert "• Total profit: $1,079.00" in report.text
    assert "• Horses: $100.00" in report.text
    assert "• Greyhounds: -$20.00" in report.text
    assert "• Soccer: $999.00" in report.text
    assert "Today (since 12:00 AM)" in report.text


def test_daily_dm_report_treats_naive_datetime_as_sydney_time() -> None:
    df = pd.DataFrame(
        [
            {
                "betId": "1",
                "eventTypeId": 7,
                "profit": 25.0,
                "settledDate": "2026-06-07T08:00:00Z",  # Sunday 18:00 local
            }
        ]
    )

    report = build_daily_dm_report_from_dataframe(
        df,
        report_dt=datetime(2026, 6, 7, 21, 0),
    )

    assert report.report_dt.tzinfo is not None
    assert report.report_dt.tzinfo.key == "Australia/Sydney"
    assert report.week_to_date.total_profit == 25.0
    assert report.day_to_date.total_profit == 25.0


def test_daily_dm_report_counts_other_sports_and_keeps_racing_lines() -> None:
    """Every sport counts; Horses and Greyhounds keep their lines on a quiet day."""
    df = pd.DataFrame(
        [
            {
                "betId": "1",
                "eventTypeId": 1,
                "profit": 75.0,
                "settledDate": "2026-06-05T20:30:00Z",  # Sat 6:30 AM local
            }
        ]
    )

    report = build_daily_dm_report_from_dataframe(
        df,
        report_dt=datetime(2026, 6, 6, 21, 0, tzinfo=SYDNEY_TZ),
    )

    assert report.week_to_date.total_profit == 75.0
    assert report.day_to_date.total_profit == 75.0
    assert report.day_to_date.horses_profit == 0.0
    assert report.day_to_date.greyhounds_profit == 0.0
    assert "• Total profit: $75.00" in report.text
    assert "• Horses: $0.00" in report.text
    assert "• Greyhounds: $0.00" in report.text
    assert "• Soccer: $75.00" in report.text


def test_daily_dm_report_heading_is_portable_and_unpadded() -> None:
    """The heading must not rely on glibc-only %-d/%-I strftime codes."""
    df = pd.DataFrame(
        [
            {
                "betId": "1",
                "eventTypeId": 7,
                "profit": 1.0,
                "settledDate": "2026-06-02T20:00:00Z",
            }
        ]
    )
    report = build_daily_dm_report_from_dataframe(
        df,
        report_dt=datetime(2026, 6, 3, 6, 5, tzinfo=SYDNEY_TZ),
    )
    assert "Wednesday 3 June, 6:05 AM" in report.text


def test_daily_dm_report_heading_noon_and_midnight() -> None:
    df = pd.DataFrame(
        [
            {
                "betId": "1",
                "eventTypeId": 7,
                "profit": 1.0,
                "settledDate": "2026-06-02T20:00:00Z",
            }
        ]
    )
    noon = build_daily_dm_report_from_dataframe(
        df, report_dt=datetime(2026, 6, 3, 12, 0, tzinfo=SYDNEY_TZ)
    )
    midnight = build_daily_dm_report_from_dataframe(
        df, report_dt=datetime(2026, 6, 3, 0, 0, tzinfo=SYDNEY_TZ)
    )
    assert "12:00 PM" in noon.text
    assert "12:00 AM" in midnight.text

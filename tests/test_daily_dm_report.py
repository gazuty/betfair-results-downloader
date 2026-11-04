from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

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

    assert "*Betfair results* · " in report.text
    assert "Friday 6 June, 9:00 PM" not in report.text
    assert "Saturday 6 June, 9:00 PM" in report.text
    assert "*Week to date* (since Sunday 12:00 AM)" in report.text
    assert (
        "• *Total: net $1,079.00* (gross $1,079.00, commission $0.00 · 0.0%)"
        in report.text
    )
    assert (
        "• Horses: net $100.00 (gross $100.00, commission $0.00 · 0.0%)" in report.text
    )
    assert (
        "• Greyhounds: net -$20.00 (gross -$20.00, commission $0.00 · n/a)"
        in report.text
    )
    assert (
        "• Soccer: net $999.00 (gross $999.00, commission $0.00 · 0.0%)" in report.text
    )
    assert "*Today* (since 12:00 AM)" in report.text


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
    assert (
        "• *Total: net $75.00* (gross $75.00, commission $0.00 · 0.0%)" in report.text
    )
    assert "• Horses: net $0.00 (gross $0.00, commission $0.00 · n/a)" in report.text
    assert (
        "• Greyhounds: net $0.00 (gross $0.00, commission $0.00 · n/a)" in report.text
    )
    assert "• Soccer: net $75.00 (gross $75.00, commission $0.00 · 0.0%)" in report.text


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


def test_daily_dm_report_yesterday_is_the_full_previous_local_day() -> None:
    """
    Yesterday is [00:00 yesterday, 00:00 today) in Sydney time: the first
    minute of yesterday is in, the first minute of today is out, and the day
    before yesterday is out. Report at Saturday 6 June 9:00 PM.
    """
    df = pd.DataFrame(
        [
            {  # Fri 5 June 00:00:30 local (Thu 14:00:30Z) -- first minute, in
                "betId": "1",
                "eventTypeId": 7,
                "profit": 10.0,
                "settledDate": "2026-06-04T14:00:30Z",
            },
            {  # Fri 5 June 23:59:00 local -- last minute, in
                "betId": "2",
                "eventTypeId": 4339,
                "profit": 5.0,
                "settledDate": "2026-06-05T13:59:00Z",
            },
            {  # Sat 6 June 00:00:00 local -- today, out of yesterday
                "betId": "3",
                "eventTypeId": 7,
                "profit": 100.0,
                "settledDate": "2026-06-05T14:00:00Z",
            },
            {  # Thu 4 June local -- day before yesterday, out
                "betId": "4",
                "eventTypeId": 1,
                "profit": 1000.0,
                "settledDate": "2026-06-04T02:00:00Z",
            },
        ]
    )

    report = build_daily_dm_report_from_dataframe(
        df, report_dt=datetime(2026, 6, 6, 21, 0, tzinfo=SYDNEY_TZ)
    )

    assert report.yesterday_start == datetime(2026, 6, 5, 0, 0, tzinfo=SYDNEY_TZ)
    assert report.yesterday is not None
    assert report.yesterday.total_profit == 15.0
    assert report.yesterday.horses_profit == 10.0
    assert report.yesterday.greyhounds_profit == 5.0
    assert report.day_to_date.total_profit == 100.0
    assert report.week_to_date.total_profit == 1115.0

    text = report.text
    assert "*Yesterday* (Friday 5 June)" in text
    assert text.index("Today") < text.index("Yesterday") < text.index("Week to date")
    yesterday_block = text[text.index("Yesterday") : text.index("Week to date")]
    assert (
        "• *Total: net $15.00* (gross $15.00, commission $0.00 · 0.0%)"
        in yesterday_block
    )
    assert (
        "• Horses: net $10.00 (gross $10.00, commission $0.00 · 0.0%)"
        in yesterday_block
    )
    assert (
        "• Greyhounds: net $5.00 (gross $5.00, commission $0.00 · 0.0%)"
        in yesterday_block
    )


def test_daily_dm_report_non_sunday_excludes_month_and_year_to_date() -> None:
    """On a non-Sunday the report omits Month to date and Year to date."""
    df = pd.DataFrame(
        [
            {
                "betId": "1",
                "eventTypeId": 7,
                "profit": 10.0,
                "settledDate": "2026-06-06T00:30:00Z",  # Sat local
            }
        ]
    )

    report = build_daily_dm_report_from_dataframe(
        df,
        report_dt=datetime(2026, 6, 6, 21, 0, tzinfo=SYDNEY_TZ),  # Saturday
    )

    assert "Month to date" not in report.text
    assert "Year to date" not in report.text
    # The dataclass fields are still populated for programmatic access
    assert report.month_to_date is not None
    assert report.year_to_date is not None


def test_daily_dm_report_sunday_includes_month_and_year_to_date() -> None:
    """On a Sunday the report includes Month to date and Year to date."""
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
        report_dt=datetime(2026, 6, 7, 21, 0, tzinfo=SYDNEY_TZ),  # Sunday
    )

    assert "Month to date" in report.text
    assert "Year to date" in report.text
    text = report.text
    # Order: Today < Yesterday < Last week < Month to date < Year to date
    assert text.index("Today") < text.index("Yesterday")
    assert text.index("Yesterday") < text.index("Last week")
    assert text.index("Last week") < text.index("Month to date")
    assert text.index("Month to date") < text.index("Year to date")


def test_daily_dm_report_yesterday_ignores_the_week_boundary() -> None:
    """On a Sunday, Yesterday is last week's Saturday; Week to date is not."""
    df = pd.DataFrame(
        [
            {  # Sat 6 June 15:00 local
                "betId": "1",
                "eventTypeId": 7,
                "profit": 40.0,
                "settledDate": "2026-06-06T05:00:00Z",
            }
        ]
    )

    report = build_daily_dm_report_from_dataframe(
        df,
        report_dt=datetime(2026, 6, 7, 6, 0, tzinfo=SYDNEY_TZ),  # Sunday
    )

    assert report.yesterday is not None
    assert report.yesterday.total_profit == 40.0
    assert report.week_to_date.total_profit == 0.0
    assert report.day_to_date.total_profit == 0.0
    assert "*Yesterday* (Saturday 6 June)" in report.text


def _bet(bet_id: str, profit: float, settled_utc: str, event_type: int = 7) -> dict:
    return {
        "betId": bet_id,
        "eventTypeId": event_type,
        "profit": profit,
        "settledDate": settled_utc,
    }


def test_sunday_report_shows_last_full_week_in_place_of_week_to_date() -> None:
    """
    Last week is [previous Sunday 12:00 AM, this Sunday 12:00 AM) in Sydney
    time. Each boundary row sits one second either side of an edge, so an
    off-by-one in either direction moves a row and changes the total.
    """
    df = pd.DataFrame(
        [
            _bet("1", 1.0, "2026-05-30T13:59:59Z"),  # Sat 30 May 23:59:59: out
            _bet("2", 2.0, "2026-05-30T14:00:00Z"),  # Sun 31 May 00:00: in
            _bet("3", 4.0, "2026-06-06T13:59:59Z"),  # Sat 6 June 23:59:59: in
            _bet("4", 8.0, "2026-06-06T14:00:00Z"),  # Sun 7 June 00:00: today
        ]
    )

    report = build_daily_dm_report_from_dataframe(
        df, report_dt=datetime(2026, 6, 7, 19, 35, tzinfo=SYDNEY_TZ)
    )

    assert report.last_week_start == datetime(2026, 5, 31, tzinfo=SYDNEY_TZ)
    assert report.last_week is not None
    assert report.last_week.total.gross == 6.0
    assert report.day_to_date.total.gross == 8.0
    assert "*Last week* (Sunday 31 May – Saturday 6 June)" in report.text
    assert "Week to date" not in report.text
    last_week_block = report.text[
        report.text.index("*Last week*") : report.text.index("*Month to date*")
    ]
    assert "• *Total: net $6.00* (gross $6.00, commission $0.00 · 0.0%)" in (
        last_week_block
    )


def test_weekday_report_shows_week_to_date_and_not_last_week() -> None:
    df = pd.DataFrame([_bet("1", 5.0, "2026-06-01T02:00:00Z")])  # Mon 1 June

    report = build_daily_dm_report_from_dataframe(
        df,
        report_dt=datetime(2026, 6, 3, 6, 10, tzinfo=SYDNEY_TZ),  # Wednesday
    )

    assert "*Week to date* (since Sunday 12:00 AM)" in report.text
    assert "Last week" not in report.text


def test_figures_lines_lead_with_net_and_mark_a_meaningless_rate_na() -> None:
    """
    Net comes first so it is read first when Slack wraps the line on a
    phone; a losing sport's rate has no meaning and reads n/a; only the
    Total line is bold.
    """
    df = pd.DataFrame(
        [
            _bet("1", 30.0, "2026-06-03T02:00:00Z"),
            _bet("2", -10.0, "2026-06-03T03:00:00Z", event_type=4339),
        ]
    )

    report = build_daily_dm_report_from_dataframe(
        df, report_dt=datetime(2026, 6, 3, 19, 35, tzinfo=SYDNEY_TZ)
    )

    today = report.text[report.text.index("*Today*") : report.text.index("*Yesterday*")]
    assert "• *Total: net $20.00* (gross $20.00, commission $0.00 · 0.0%)" in today
    assert "• Horses: net $30.00 (gross $30.00, commission $0.00 · 0.0%)" in today
    assert (
        "• Greyhounds: net -$10.00 (gross -$10.00, commission $0.00 · n/a)\n" in today
    )
    assert today.count("n/a") == 1, "only the losing sport has no rate"
    assert report.text.count("• *") == report.text.count("• *Total:")


def test_report_opens_with_title_and_timestamp_on_one_line() -> None:
    """A Slack push notification previews only the start of the message."""
    report = build_daily_dm_report_from_dataframe(
        pd.DataFrame([_bet("1", 1.0, "2026-06-03T02:00:00Z")]),
        report_dt=datetime(2026, 6, 3, 6, 10, tzinfo=SYDNEY_TZ),
    )

    assert report.text.splitlines()[0] == (
        "*Betfair results* · Wednesday 3 June, 6:10 AM"
    )


def test_every_line_has_balanced_slack_bold_markers() -> None:
    """An odd ``*`` count on a line leaves Slack showing a stray asterisk."""
    report = build_daily_dm_report_from_dataframe(
        pd.DataFrame([_bet("1", 1.0, "2026-06-06T02:00:00Z")]),
        report_dt=datetime(2026, 6, 7, 19, 35, tzinfo=SYDNEY_TZ),  # Sunday
    )

    for line in report.text.splitlines():
        assert line.count("*") % 2 == 0, line


@pytest.mark.parametrize(
    ("report_day", "rows", "expected_start"),
    [
        # DST ends Sun 5 April 2026 at 3:00 AM: the window opens at +11 and
        # closes at +10.
        (
            datetime(2026, 4, 12, 19, 35, tzinfo=SYDNEY_TZ),
            [
                ("1", 1.0, "2026-04-04T12:59:59Z"),  # Sat 4 Apr 23:59:59 +11: out
                ("2", 2.0, "2026-04-04T13:00:00Z"),  # Sun 5 Apr 00:00 +11: in
                ("3", 4.0, "2026-04-11T13:59:59Z"),  # Sat 11 Apr 23:59:59 +10: in
                ("4", 8.0, "2026-04-11T14:00:00Z"),  # Sun 12 Apr 00:00 +10: today
            ],
            datetime(2026, 4, 5, tzinfo=SYDNEY_TZ),
        ),
        # DST starts Sun 4 October 2026 at 2:00 AM: +10 to +11.
        (
            datetime(2026, 10, 11, 19, 35, tzinfo=SYDNEY_TZ),
            [
                ("1", 1.0, "2026-10-03T13:59:59Z"),  # Sat 3 Oct 23:59:59 +10: out
                ("2", 2.0, "2026-10-03T14:00:00Z"),  # Sun 4 Oct 00:00 +10: in
                ("3", 4.0, "2026-10-10T12:59:59Z"),  # Sat 10 Oct 23:59:59 +11: in
                ("4", 8.0, "2026-10-10T13:00:00Z"),  # Sun 11 Oct 00:00 +11: today
            ],
            datetime(2026, 10, 4, tzinfo=SYDNEY_TZ),
        ),
    ],
    ids=["dst-ends", "dst-starts"],
)
def test_last_week_window_spans_a_daylight_saving_change(
    report_day: datetime, rows: list, expected_start: datetime
) -> None:
    """
    Sydney changes its clocks on a Sunday, so a week window spans the
    change twice a year. Both edges must stay at local midnight; arithmetic
    done in UTC or at a fixed offset would move one of them by an hour.
    """
    df = pd.DataFrame([_bet(*row) for row in rows])

    report = build_daily_dm_report_from_dataframe(df, report_dt=report_day)

    assert report.last_week_start == expected_start
    assert report.last_week is not None
    assert report.last_week.total.gross == 6.0
    assert report.day_to_date.total.gross == 8.0

"""
The daily report and the market settlement status file.

Betfair settles the losers of an outright market as they are eliminated, so
"settled" rows arrive for a market whose outcome is still open. The report
must hold those back until the pipeline has seen the market CLOSED, and then
count the whole market on the day it closed -- not scatter its legs across
weeks that were already reported.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd

from betfair_results_downloader.market_status import STATUS_COLUMNS, STATUS_FILENAME
from betfair_results_downloader.reporting.daily_dm_report import (
    apply_settlement_status,
    build_daily_dm_report_from_dataframe,
)
from betfair_results_downloader.reporting.schema import (
    normalize_cleared_orders_schema,
)

SYDNEY = ZoneInfo("Australia/Sydney")
# Saturday 6 June 2026, 9:00 PM Sydney. The week started Sunday 31 May.
REPORT_AT = datetime(2026, 6, 6, 21, 0, tzinfo=SYDNEY)


def _row(bet_id: str, market_id: str, event_type: int, profit: float, settled: str):
    return {
        "betId": bet_id,
        "marketId": market_id,
        "eventTypeId": event_type,
        "profit": profit,
        "settledDate": settled,
    }


def _status(
    market_id: str,
    status: str,
    *,
    first_pending: str = "",
    closed_observed: str = "",
    source: str = "book",
) -> dict:
    return {
        "marketId": market_id,
        "status": status,
        "activeRunners": "0" if status == "CLOSED" else "5",
        "source": source,
        "checkedUtc": "2026-06-06T09:00:00Z",
        "firstPendingUtc": first_pending,
        "closedObservedUtc": closed_observed,
    }


def _status_frame(*rows: dict) -> pd.DataFrame:
    return pd.DataFrame(list(rows), columns=STATUS_COLUMNS, dtype=str)


def test_pending_market_is_held_out_of_totals_and_shown_as_pending() -> None:
    df = pd.DataFrame(
        [
            _row("1", "1.100", 7, 10.0, "2026-06-06T02:00:00Z"),  # Sat noon local
            _row("2", "1.200", 2, 5.10, "2026-06-05T20:18:53Z"),  # outright leg
            _row("3", "1.200", 2, 7.15, "2026-06-06T01:33:27Z"),  # another leg
        ]
    )
    status = _status_frame(
        _status("1.100", "CLOSED", closed_observed="2026-06-06T09:00:00Z"),
        _status("1.200", "OPEN", first_pending="2026-06-05T21:00:00Z"),
    )

    report = build_daily_dm_report_from_dataframe(
        df, report_dt=REPORT_AT, market_status=status
    )

    assert report.week_to_date.total_profit == 10.0
    assert report.day_to_date.total_profit == 10.0
    assert report.pending.markets == 1
    assert report.pending.profit == 12.25
    assert "• Tennis" not in report.text
    assert "Pending (partially settled, not counted above)" in report.text
    assert "• 1 market, $12.25 settled so far — each counts in full" in report.text


def test_market_that_closed_after_being_pending_counts_on_its_close_day() -> None:
    """
    Legs settled last week (eliminations) plus the market closing this
    Saturday: the whole market belongs to Saturday, so it shows in both
    Today and Week to date even though no leg settled today.
    """
    df = pd.DataFrame(
        [
            _row("1", "1.300", 2, 0.23, "2026-05-28T20:18:53Z"),  # prior week
            _row("2", "1.300", 2, 5.10, "2026-06-01T06:27:24Z"),  # Monday
            _row("3", "1.300", 2, 40.0, "2026-06-06T04:30:00Z"),  # the final
        ]
    )
    status = _status_frame(
        _status(
            "1.300",
            "CLOSED",
            first_pending="2026-05-29T21:00:00Z",
            closed_observed="2026-06-06T05:00:00Z",  # Sat 3:00 PM Sydney
        )
    )

    report = build_daily_dm_report_from_dataframe(
        df, report_dt=REPORT_AT, market_status=status
    )

    assert report.day_to_date.total_profit == 45.33
    assert report.week_to_date.total_profit == 45.33
    assert ("Tennis", 45.33) in report.day_to_date.by_sport
    assert report.pending.markets == 0
    assert "• None" in report.text


def test_market_closed_at_first_sight_keeps_its_settle_dates() -> None:
    """
    Racing and match markets are CLOSED the first time the pipeline looks.
    Their closedObservedUtc is a pipeline run time, hours after settlement;
    it must not move a Wednesday result into Saturday's Today section.
    """
    df = pd.DataFrame([_row("1", "1.400", 7, 30.0, "2026-06-03T05:00:00Z")])  # Wed
    status = _status_frame(
        _status("1.400", "CLOSED", closed_observed="2026-06-06T05:00:00Z")
    )

    report = build_daily_dm_report_from_dataframe(
        df, report_dt=REPORT_AT, market_status=status
    )

    assert report.week_to_date.horses_profit == 30.0
    assert report.day_to_date.horses_profit == 0.0


def test_markets_without_a_status_record_count_as_final() -> None:
    """
    No record means the status step has not seen the market (a failed run,
    or a backfill of old dates). Counting it is exactly the pre-feature
    behaviour, so a status outage degrades to the old report, never to $0.
    """
    df = pd.DataFrame([_row("1", "1.500", 1, 75.0, "2026-06-06T02:00:00Z")])
    status = _status_frame(
        _status("1.999", "OPEN", first_pending="2026-06-01T00:00:00Z")
    )

    report = build_daily_dm_report_from_dataframe(
        df, report_dt=REPORT_AT, market_status=status
    )

    assert report.day_to_date.total_profit == 75.0
    assert ("Soccer", 75.0) in report.day_to_date.by_sport
    assert report.pending.markets == 0


def test_no_status_file_reports_everything_as_before() -> None:
    df = pd.DataFrame(
        [
            _row("1", "1.100", 7, 10.0, "2026-06-06T02:00:00Z"),
            _row("2", "1.200", 2, 12.25, "2026-06-06T01:33:27Z"),
        ]
    )

    without = build_daily_dm_report_from_dataframe(df, report_dt=REPORT_AT)
    with_empty = build_daily_dm_report_from_dataframe(
        df, report_dt=REPORT_AT, market_status=_status_frame()
    )

    assert without.day_to_date.total_profit == 22.25
    assert with_empty.day_to_date.total_profit == 22.25
    assert without.pending.markets == 0
    assert "• None" in without.text


def test_float_damaged_market_spelling_still_matches_its_status_row() -> None:
    """8.6% of historical canonical rows spell 1.251500100 as 1.2515001."""
    df = pd.DataFrame([_row("1", "1.2515001", 2, 12.25, "2026-06-06T01:33:27Z")])
    status = _status_frame(
        _status("1.251500100", "OPEN", first_pending="2026-06-05T21:00:00Z")
    )

    report = build_daily_dm_report_from_dataframe(
        df, report_dt=REPORT_AT, market_status=status
    )

    assert report.day_to_date.total_profit == 0.0
    assert report.pending.markets == 1


def test_suspended_and_inactive_count_as_pending() -> None:
    df = pd.DataFrame(
        [
            _row("1", "1.600", 1, 5.0, "2026-06-06T02:00:00Z"),
            _row("2", "1.700", 1, 6.0, "2026-06-06T02:00:00Z"),
        ]
    )
    status = _status_frame(
        _status("1.600", "SUSPENDED", first_pending="2026-06-06T03:00:00Z"),
        _status("1.700", "INACTIVE", first_pending="2026-06-06T03:00:00Z"),
    )

    report = build_daily_dm_report_from_dataframe(
        df, report_dt=REPORT_AT, market_status=status
    )

    assert report.day_to_date.total_profit == 0.0
    assert report.pending.markets == 2
    assert "• 2 markets, $11.00 settled so far" in report.text


def test_pending_legs_settled_after_the_report_time_are_not_counted() -> None:
    """--at on a historical timestamp must not see the future, pending or not."""
    df = pd.DataFrame(
        [
            _row("1", "1.200", 2, 5.0, "2026-06-06T01:00:00Z"),  # before 9 PM
            _row("2", "1.200", 2, 7.0, "2026-06-06T12:00:00Z"),  # Sun 10 PM local
        ]
    )
    status = _status_frame(
        _status("1.200", "OPEN", first_pending="2026-06-05T21:00:00Z")
    )

    report = build_daily_dm_report_from_dataframe(
        df, report_dt=REPORT_AT, market_status=status
    )

    assert report.pending.profit == 5.0


def test_sport_lines_keep_horses_and_greyhounds_first_then_by_magnitude() -> None:
    df = pd.DataFrame(
        [
            _row("1", "1.1", 2, 12.0, "2026-06-06T02:00:00Z"),  # Tennis
            _row("2", "1.2", 1, -50.0, "2026-06-06T02:00:00Z"),  # Soccer
            _row("3", "1.3", 61420, 3.0, "2026-06-06T02:00:00Z"),  # Australian Rules
        ]
    )

    report = build_daily_dm_report_from_dataframe(df, report_dt=REPORT_AT)

    labels = [label for label, _ in report.day_to_date.by_sport]
    assert labels == ["Horses", "Greyhounds", "Soccer", "Tennis", "Australian Rules"]
    text = report.text
    assert text.index("• Horses: $0.00") < text.index("• Greyhounds: $0.00")
    assert text.index("• Soccer: -$50.00") < text.index("• Tennis: $12.00")
    assert text.index("• Tennis: $12.00") < text.index("• Australian Rules: $3.00")
    assert report.day_to_date.total_profit == -35.0


def test_apply_settlement_status_redates_only_previously_pending_closed_markets() -> (
    None
):
    normalized = normalize_cleared_orders_schema(
        pd.DataFrame(
            [
                _row("1", "1.300", 2, 1.0, "2026-06-01T06:00:00Z"),  # was pending
                _row("2", "1.400", 7, 2.0, "2026-06-01T06:00:00Z"),  # closed at once
                _row("3", "1.200", 2, 3.0, "2026-06-01T06:00:00Z"),  # still open
            ]
        )
    )
    status = _status_frame(
        _status(
            "1.300",
            "CLOSED",
            first_pending="2026-05-29T21:00:00Z",
            closed_observed="2026-06-06T05:00:00Z",
        ),
        _status("1.400", "CLOSED", closed_observed="2026-06-06T05:00:00Z"),
        _status("1.200", "OPEN", first_pending="2026-05-29T21:00:00Z"),
    )

    final, pending = apply_settlement_status(normalized, status)

    by_bet = final.set_index("betId")["settled_dt_local"]
    assert by_bet["1"] == pd.Timestamp("2026-06-06T05:00:00Z").tz_convert(SYDNEY)
    assert by_bet["2"] == pd.Timestamp("2026-06-01T06:00:00Z").tz_convert(SYDNEY)
    assert list(pending["betId"]) == ["3"]


def test_report_at_a_time_before_the_observed_close_treats_market_as_pending() -> None:
    """
    --at re-renders a past moment. A market whose close was observed later
    than that moment was, as of then, still pending: its money must appear
    in Pending, not vanish from both sections.
    """
    df = pd.DataFrame(
        [
            _row("1", "1.300", 2, 5.0, "2026-06-01T06:00:00Z"),
            _row("2", "1.300", 2, 40.0, "2026-06-06T04:30:00Z"),
        ]
    )
    status = _status_frame(
        _status(
            "1.300",
            "CLOSED",
            first_pending="2026-05-29T21:00:00Z",
            closed_observed="2026-06-06T05:00:00Z",  # Sat 3:00 PM Sydney
        )
    )
    before_close = datetime(2026, 6, 6, 10, 0, tzinfo=SYDNEY)

    report = build_daily_dm_report_from_dataframe(
        df, report_dt=before_close, market_status=status
    )

    assert report.week_to_date.total_profit == 0.0
    assert report.day_to_date.total_profit == 0.0
    assert report.pending.markets == 1
    assert report.pending.profit == 5.0, "only the leg settled before --at"


def test_pending_line_accumulates_legs_from_before_the_week() -> None:
    """The pending amount is the market's life so far, not the week's slice."""
    df = pd.DataFrame(
        [
            _row("1", "1.200", 2, 100.0, "2026-05-20T06:00:00Z"),  # weeks ago
            _row("2", "1.200", 2, 12.25, "2026-06-06T01:33:27Z"),
        ]
    )
    status = _status_frame(
        _status("1.200", "OPEN", first_pending="2026-05-20T07:00:00Z")
    )

    report = build_daily_dm_report_from_dataframe(
        df, report_dt=REPORT_AT, market_status=status
    )

    assert report.pending.profit == 112.25
    assert "$112.25 settled so far — each counts in full on the day it closes" in (
        report.text
    )


def test_duplicate_status_rows_resolve_to_the_last_one() -> None:
    df = pd.DataFrame([_row("1", "1.200", 2, 12.25, "2026-06-06T01:33:27Z")])
    status = _status_frame(
        _status("1.200", "OPEN", first_pending="2026-06-01T00:00:00Z"),
        _status(
            "1.200",
            "CLOSED",
            first_pending="2026-06-01T00:00:00Z",
            closed_observed="2026-06-06T05:00:00Z",
        ),
    )

    report = build_daily_dm_report_from_dataframe(
        df, report_dt=REPORT_AT, market_status=status
    )

    assert report.day_to_date.total_profit == 12.25
    assert report.pending.markets == 0


def test_blank_status_is_conservatively_pending() -> None:
    df = pd.DataFrame([_row("1", "1.200", 2, 12.25, "2026-06-06T01:33:27Z")])
    status = _status_frame(_status("1.200", ""))

    report = build_daily_dm_report_from_dataframe(
        df, report_dt=REPORT_AT, market_status=status
    )

    assert report.day_to_date.total_profit == 0.0
    assert report.pending.markets == 1


def test_status_row_without_market_id_cannot_hold_back_blank_market_rows() -> None:
    """
    Real canonical rows can carry an empty marketId. A blank status row must
    not match them all on the shared "" key and suppress their profit.
    """
    df = pd.DataFrame(
        [
            _row("1", "", 7, 10.0, "2026-06-06T02:00:00Z"),
            _row("2", "", 7, 5.0, "2026-06-06T02:00:00Z"),
        ]
    )
    status = _status_frame(_status("", "OPEN", first_pending="2026-06-01T00:00:00Z"))

    report = build_daily_dm_report_from_dataframe(
        df, report_dt=REPORT_AT, market_status=status
    )

    assert report.day_to_date.total_profit == 15.0
    assert report.pending.markets == 0


def test_redated_rows_keep_settled_date_local_consistent() -> None:
    normalized = normalize_cleared_orders_schema(
        pd.DataFrame([_row("1", "1.300", 2, 1.0, "2026-06-01T06:00:00Z")])
    )
    status = _status_frame(
        _status(
            "1.300",
            "CLOSED",
            first_pending="2026-05-29T21:00:00Z",
            closed_observed="2026-06-06T05:00:00Z",
        )
    )

    final, _ = apply_settlement_status(normalized, status)

    row = final.iloc[0]
    assert row["settled_dt_local"].date() == row["settled_date_local"]
    assert str(row["settled_date_local"]) == "2026-06-06"


def _write_results_dir(tmp_path, *, status_rows: list[dict]) -> None:
    (tmp_path / ".cache").mkdir(parents=True)
    (tmp_path / "cleared_orders_cleaned.csv").write_text(
        "betId,marketId,eventTypeId,profit,settledDate\n"
        "1,1.100,7,10.0,2026-06-06T02:00:00Z\n"  # today's race
        "2,1.200,2,7.25,2026-06-06T01:33:27Z\n",  # outright, recent leg
        encoding="utf-8",
    )
    pd.DataFrame(
        [
            _row("3", "1.200", 2, 100.0, "2025-04-01T10:00:00Z"),  # archived leg
            _row("4", "1.900", 1, 999.0, "2025-04-01T10:00:00Z"),  # unrelated
            _row("2", "1.200", 2, 7.25, "2026-06-06T01:33:27Z"),  # already held
        ],
        dtype=str,
    ).to_csv(tmp_path / "cleared_orders_archive_2025.csv.gz", index=False)
    _status_frame(*status_rows).to_csv(
        tmp_path / ".cache" / STATUS_FILENAME, index=False
    )


def test_archived_legs_of_a_pending_market_count_towards_pending(tmp_path) -> None:
    """
    A market partially settled for longer than the archive window has its
    early legs in the yearly archives, not the canonical. The pending amount
    must still be the whole market.
    """
    from betfair_results_downloader.reporting.daily_dm_report import (
        build_daily_dm_report_from_results_dir,
    )

    _write_results_dir(
        tmp_path,
        status_rows=[_status("1.200", "OPEN", first_pending="2025-04-02T00:00:00Z")],
    )

    report = build_daily_dm_report_from_results_dir(str(tmp_path), report_dt=REPORT_AT)

    assert report.pending.markets == 1
    assert report.pending.profit == 107.25, "archived leg included once"
    assert report.day_to_date.total_profit == 10.0, "unrelated archived row ignored"


def test_archived_legs_count_in_full_on_the_day_the_market_closes(tmp_path) -> None:
    from betfair_results_downloader.reporting.daily_dm_report import (
        build_daily_dm_report_from_results_dir,
    )

    _write_results_dir(
        tmp_path,
        status_rows=[
            _status(
                "1.200",
                "CLOSED",
                first_pending="2025-04-02T00:00:00Z",
                closed_observed="2026-06-06T05:00:00Z",
            )
        ],
    )

    report = build_daily_dm_report_from_results_dir(str(tmp_path), report_dt=REPORT_AT)

    assert report.day_to_date.total_profit == 117.25
    assert ("Tennis", 107.25) in report.day_to_date.by_sport
    assert report.pending.markets == 0

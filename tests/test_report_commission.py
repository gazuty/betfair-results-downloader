"""
The daily report and the market commission store.

Betfair charges commission per market on its net winnings, so the report
shows gross, commission and net per sport and in total for each window, with
the percentage derived at that level only. A market whose commission cannot
be trusted is counted at $0.00 and said out loud.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd

from betfair_results_downloader.commission import (
    COMMISSION_COLUMNS,
    COMMISSION_FILENAME,
)
from betfair_results_downloader.market_status import STATUS_COLUMNS, STATUS_FILENAME
from betfair_results_downloader.reporting.daily_dm_report import (
    SportFigures,
    build_daily_dm_report_from_dataframe,
    build_daily_dm_report_from_results_dir,
)

SYDNEY = ZoneInfo("Australia/Sydney")
# Saturday 6 June 2026, 9:00 PM Sydney. Week started Sunday 31 May; month
# started Monday 1 June; year started Thursday 1 January.
REPORT_AT = datetime(2026, 6, 6, 21, 0, tzinfo=SYDNEY)


def _row(bet_id: str, market_id: str, event_type: int, profit: float, settled: str):
    return {
        "betId": bet_id,
        "marketId": market_id,
        "eventTypeId": event_type,
        "profit": profit,
        "settledDate": settled,
    }


def _commission(
    market_id: str,
    amount: str,
    fetched="2026-06-06T10:00:00Z",
    settled="2026-06-06T02:00:00Z",
) -> dict:
    return {
        "marketId": market_id,
        "eventTypeId": "7",
        "settledDateUtc": settled,
        "betCount": "1",
        "grossProfit": "0.00",
        "commission": amount,
        "fetchedUtc": fetched,
    }


def _commission_frame(*rows: dict) -> pd.DataFrame:
    return pd.DataFrame(list(rows), columns=COMMISSION_COLUMNS, dtype=str)


def _status(market_id: str, status: str, first_pending="", closed="") -> dict:
    return {
        "marketId": market_id,
        "status": status,
        "activeRunners": "0" if status == "CLOSED" else "5",
        "source": "book",
        "checkedUtc": "2026-06-06T09:00:00Z",
        "firstPendingUtc": first_pending,
        "closedObservedUtc": closed,
    }


def _status_frame(*rows: dict) -> pd.DataFrame:
    return pd.DataFrame(list(rows), columns=STATUS_COLUMNS, dtype=str)


def _by_label(figures: tuple[SportFigures, ...]) -> dict[str, SportFigures]:
    return {f.label: f for f in figures}


def test_gross_commission_net_and_percentage_per_sport_and_in_total() -> None:
    df = pd.DataFrame(
        [
            _row("1", "1.100", 7, 100.0, "2026-06-06T02:00:00Z"),  # Sat noon local
            _row("2", "1.100", 7, -20.0, "2026-06-06T02:00:00Z"),  # same market
            _row("3", "1.200", 4339, 50.0, "2026-06-06T03:00:00Z"),
            _row("4", "1.300", 1, -30.0, "2026-06-06T04:00:00Z"),  # losing soccer
        ]
    )
    store = _commission_frame(
        _commission("1.100", "5.60"),
        _commission("1.200", "3.50", settled="2026-06-06T03:00:00Z"),
        _commission("1.300", "0.00", settled="2026-06-06T04:00:00Z"),
    )

    report = build_daily_dm_report_from_dataframe(
        df, report_dt=REPORT_AT, market_commission=store
    )

    today = report.day_to_date
    assert today.total.gross == 100.0
    assert today.total.commission == 9.1
    assert today.total.net == 90.9
    assert today.total.commission_pct == 9.1
    sports = _by_label(today.by_sport)
    assert sports["Horses"].gross == 80.0 and sports["Horses"].commission == 5.6
    assert sports["Horses"].commission_pct == 7.0
    assert sports["Greyhounds"].net == 46.5
    assert sports["Soccer"].commission_pct is None, "a losing sport has no rate"
    assert today.total.unknown_markets == 0

    text = report.text
    assert "• Total: gross $100.00, commission $9.10 (9.1%), net $90.90" in text
    assert "• Horses: gross $80.00, commission $5.60 (7.0%), net $74.40" in text
    assert "• Greyhounds: gross $50.00, commission $3.50 (7.0%), net $46.50" in text
    assert "• Soccer: gross -$30.00, commission $0.00 (n/a), net -$30.00" in text
    assert "Commission unknown" not in text
    # Gross accessors are unchanged for callers that only want the old figure.
    assert today.total_profit == 100.0 and today.horses_profit == 80.0


def test_markets_without_a_commission_row_are_counted_and_announced() -> None:
    df = pd.DataFrame(
        [
            _row("1", "1.100", 7, 10.0, "2026-06-06T02:00:00Z"),
            _row("2", "1.200", 7, 20.0, "2026-06-06T02:00:00Z"),
            _row("3", "1.300", 2, 5.0, "2026-06-06T02:00:00Z"),
        ]
    )
    store = _commission_frame(_commission("1.100", "0.70"))

    report = build_daily_dm_report_from_dataframe(
        df, report_dt=REPORT_AT, market_commission=store
    )

    today = report.day_to_date
    assert today.total.commission == 0.7
    assert today.total.unknown_markets == 2
    sports = _by_label(today.by_sport)
    assert sports["Horses"].unknown_markets == 1
    assert sports["Tennis"].unknown_markets == 1
    assert "• ⚠️ Commission unknown for 2 markets (counted as $0.00)" in report.text
    # No rate is stated over a partly missing numerator, at either level.
    assert today.total.commission_pct is None
    assert sports["Horses"].commission_pct is None
    assert sports["Tennis"].commission_pct is None
    assert "• Total: gross $35.00, commission $0.70 (n/a), net $34.30" in report.text


def test_rows_without_a_market_id_are_commission_unknown() -> None:
    """
    A row with a blank marketId counts in gross but can never match the
    store, so it must show as unknown rather than as a commission-free win.
    """
    df = pd.DataFrame(
        [
            _row("1", "1.100", 7, 10.0, "2026-06-06T02:00:00Z"),
            _row("2", "", 7, 100.0, "2026-06-06T02:00:00Z"),
            _row("3", None, 2, 5.0, "2026-06-06T02:00:00Z"),
        ]
    )
    store = _commission_frame(_commission("1.100", "0.70"))

    report = build_daily_dm_report_from_dataframe(
        df, report_dt=REPORT_AT, market_commission=store
    )

    today = report.day_to_date
    assert today.total.gross == 115.0
    assert today.total.commission == 0.7
    assert today.total.unknown_markets == 2
    assert today.total.commission_pct is None
    sports = _by_label(today.by_sport)
    assert (
        sports["Horses"].unknown_markets == 1 and sports["Tennis"].unknown_markets == 1
    )
    assert "• ⚠️ Commission unknown for 2 markets (counted as $0.00)" in report.text


def test_no_store_at_all_marks_every_market_unknown() -> None:
    df = pd.DataFrame([_row("1", "1.100", 7, 10.0, "2026-06-06T02:00:00Z")])

    report = build_daily_dm_report_from_dataframe(df, report_dt=REPORT_AT)

    assert report.day_to_date.total.unknown_markets == 1
    assert "• ⚠️ Commission unknown for 1 market (counted as $0.00)" in report.text


def test_a_read_taken_before_the_close_is_still_a_placeholder() -> None:
    """
    A partially settled market reports 0.0 until it closes. A row read
    before the status step saw the close is Betfair's placeholder, not the
    charge; a row read after it is final. A market never seen pending is
    trusted on its first read.
    """
    df = pd.DataFrame(
        [
            _row("1", "1.100", 2, 40.0, "2026-06-01T06:00:00Z"),  # outright leg
            _row("2", "1.100", 2, 5.0, "2026-06-06T04:30:00Z"),  # the final
            _row("3", "1.200", 2, 12.0, "2026-06-06T04:30:00Z"),  # another final
            _row("4", "1.300", 7, 8.0, "2026-06-06T04:30:00Z"),  # racing
        ]
    )
    status = _status_frame(
        _status(
            "1.100",
            "CLOSED",
            first_pending="2026-05-29T21:00:00Z",
            closed="2026-06-06T05:00:00Z",
        ),
        _status(
            "1.200",
            "CLOSED",
            first_pending="2026-05-29T21:00:00Z",
            closed="2026-06-06T05:00:00Z",
        ),
        _status("1.300", "CLOSED", closed="2026-06-06T05:00:00Z"),
    )
    store = _commission_frame(
        _commission(
            "1.100",
            "0.00",
            fetched="2026-06-06T04:45:00Z",
            settled="2026-06-06T04:30:00Z",
        ),  # before close
        _commission(
            "1.200",
            "0.64",
            fetched="2026-06-06T05:30:00Z",
            settled="2026-06-06T04:30:00Z",
        ),  # after close
        _commission(
            "1.300",
            "0.56",
            fetched="2026-06-06T04:45:00Z",
            settled="2026-06-06T04:30:00Z",
        ),  # never pending
    )

    report = build_daily_dm_report_from_dataframe(
        df, report_dt=REPORT_AT, market_status=status, market_commission=store
    )

    today = report.day_to_date
    sports = _by_label(today.by_sport)
    assert today.total.gross == 65.0, "the closed outright counts in full today"
    assert sports["Tennis"].commission == 0.64
    assert sports["Tennis"].unknown_markets == 1
    assert sports["Horses"].commission == 0.56
    assert sports["Horses"].unknown_markets == 0
    assert "• ⚠️ Commission unknown for 1 market (counted as $0.00)" in report.text


def test_commission_follows_the_market_to_the_window_of_its_latest_leg() -> None:
    """
    Betfair places the grouped row at the latest leg, so the whole market's
    commission belongs to the window containing that leg. Gross stays per
    bet row: yesterday's leg is yesterday's gross, today's is today's.
    """
    df = pd.DataFrame(
        [
            _row("1", "1.100", 7, 30.0, "2026-06-05T02:00:00Z"),  # Fri noon local
            _row("2", "1.100", 7, 10.0, "2026-06-06T02:00:00Z"),  # Sat noon local
        ]
    )
    store = _commission_frame(_commission("1.100", "2.80"))

    report = build_daily_dm_report_from_dataframe(
        df, report_dt=REPORT_AT, market_commission=store
    )

    assert report.yesterday is not None
    assert report.yesterday.total.gross == 30.0
    assert report.yesterday.total.commission == 0.0
    assert report.day_to_date.total.gross == 10.0
    assert report.day_to_date.total.commission == 2.8
    assert report.week_to_date.total.gross == 40.0
    assert report.week_to_date.total.commission == 2.8
    assert report.week_to_date.total.commission_pct == 7.0


def test_a_report_for_an_earlier_time_does_not_borrow_a_later_commission() -> None:
    """
    Rendered with --at before a market's final leg, the canonical rows after
    the cutoff are gone. The store's figure was read after that final leg,
    so it did not exist at the report time: placed on the store's own
    settlement, it falls after the cutoff and the market is unknown, rather
    than landing its final commission on an earlier leg.
    """
    df = pd.DataFrame(
        [
            _row("1", "1.100", 7, 30.0, "2026-06-06T13:50:00Z"),  # Sat 11:50 PM local
            _row("2", "1.100", 7, 10.0, "2026-06-06T14:10:00Z"),  # Sun 12:10 AM local
        ]
    )
    store = _commission_frame(
        _commission(
            "1.100",
            "2.80",
            fetched="2026-06-06T15:00:00Z",
            settled="2026-06-06T14:10:00Z",
        )
    )

    before = build_daily_dm_report_from_dataframe(
        df,
        report_dt=datetime(2026, 6, 6, 23, 55, tzinfo=SYDNEY),
        market_commission=store,
    )
    after = build_daily_dm_report_from_dataframe(
        df, report_dt=datetime(2026, 6, 7, 9, 0, tzinfo=SYDNEY), market_commission=store
    )

    assert before.day_to_date.total.gross == 30.0
    assert before.day_to_date.total.commission == 0.0
    assert before.day_to_date.total.unknown_markets == 1
    assert before.day_to_date.total.commission_pct is None
    assert after.yesterday is not None
    assert (
        after.yesterday.total.gross == 30.0 and after.yesterday.total.commission == 0.0
    )
    assert (
        after.day_to_date.total.gross == 10.0
        and after.day_to_date.total.commission == 2.8
    )
    assert after.day_to_date.total.unknown_markets == 0


def test_a_store_row_older_than_the_canonical_is_a_stale_read() -> None:
    """A store row that predates a leg the canonical holds cannot be final."""
    df = pd.DataFrame(
        [
            _row("1", "1.100", 7, 30.0, "2026-06-05T02:00:00Z"),
            _row("2", "1.100", 7, 10.0, "2026-06-06T02:00:00Z"),
        ]
    )
    store = _commission_frame(
        _commission(
            "1.100",
            "2.10",
            fetched="2026-06-05T03:00:00Z",
            settled="2026-06-05T02:00:00Z",
        )
    )

    report = build_daily_dm_report_from_dataframe(
        df, report_dt=REPORT_AT, market_commission=store
    )

    assert report.day_to_date.total.commission == 0.0
    assert report.day_to_date.total.unknown_markets == 1
    assert report.week_to_date.total.unknown_markets == 1


def test_pending_markets_carry_no_commission_into_the_totals() -> None:
    df = pd.DataFrame(
        [
            _row("1", "1.100", 7, 10.0, "2026-06-06T02:00:00Z"),
            _row("2", "1.200", 2, 500.0, "2026-06-06T02:00:00Z"),  # still open
        ]
    )
    status = _status_frame(
        _status("1.100", "CLOSED", closed="2026-06-06T05:00:00Z"),
        _status("1.200", "OPEN", first_pending="2026-06-05T21:00:00Z"),
    )
    store = _commission_frame(
        # A real-looking charge on the pending market: if gating failed to
        # hold it out, the total would be 35.70 rather than 0.70.
        _commission("1.100", "0.70"),
        _commission("1.200", "35.00"),
    )

    report = build_daily_dm_report_from_dataframe(
        df, report_dt=REPORT_AT, market_status=status, market_commission=store
    )

    assert report.day_to_date.total.gross == 10.0
    assert report.day_to_date.total.commission == 0.7
    assert report.day_to_date.total.unknown_markets == 0
    assert report.pending.markets == 1


def test_month_and_year_to_date_windows_and_headings() -> None:
    df = pd.DataFrame(
        [
            _row("1", "1.100", 7, 10.0, "2026-06-06T02:00:00Z"),  # Sat 6 June: all
            _row(
                "2", "1.200", 7, 20.0, "2026-05-30T02:00:00Z"
            ),  # Sat 30 May: year only
            _row(
                "3", "1.300", 7, 40.0, "2026-06-01T02:00:00Z"
            ),  # Mon 1 June: month, week
            _row(
                "4", "1.400", 7, 80.0, "2025-12-31T14:00:00Z"
            ),  # 1 Jan 1:00 AM local: year
            _row("5", "1.500", 7, 160.0, "2025-12-31T12:00:00Z"),  # 31 Dec local: out
        ]
    )
    store = _commission_frame(
        _commission("1.100", "0.70"),
        _commission("1.200", "1.40", settled="2026-05-30T02:00:00Z"),
        _commission("1.300", "2.80", settled="2026-06-01T02:00:00Z"),
        _commission("1.400", "5.60", settled="2025-12-31T14:00:00Z"),
        _commission("1.500", "11.20", settled="2025-12-31T12:00:00Z"),
    )

    report = build_daily_dm_report_from_dataframe(
        df, report_dt=REPORT_AT, market_commission=store
    )

    assert report.month_start == datetime(2026, 6, 1, 0, 0, tzinfo=SYDNEY)
    assert report.year_start == datetime(2026, 1, 1, 0, 0, tzinfo=SYDNEY)
    assert report.month_to_date is not None and report.year_to_date is not None
    assert report.week_to_date.total.gross == 50.0
    assert report.month_to_date.total.gross == 50.0
    assert report.month_to_date.total.commission == 3.5
    assert report.year_to_date.total.gross == 150.0
    assert report.year_to_date.total.commission == 10.5
    assert report.year_to_date.total.commission_pct == 7.0

    text = report.text
    assert "Month to date (since Monday 1 June)" in text
    assert "Year to date (since Thursday 1 January)" in text
    assert (
        text.index("Today (since 12:00 AM)")
        < text.index("Month to date")
        < text.index("Year to date")
        < text.index("Pending (partially settled")
    )
    year_block = text[text.index("Year to date") : text.index("Pending (")]
    assert "• Total: gross $150.00, commission $10.50 (7.0%), net $139.50" in year_block


def test_report_reads_the_commission_store_beside_the_csv(tmp_path) -> None:
    (tmp_path / ".cache").mkdir()
    (tmp_path / "cleared_orders_cleaned.csv").write_text(
        "betId,marketId,eventTypeId,profit,settledDate\n"
        "1,1.100,7,12.5,2026-06-06T00:30:00Z\n",
        encoding="utf-8",
    )
    _commission_frame(
        _commission("1.100", "0.88", settled="2026-06-06T00:30:00Z")
    ).to_csv(tmp_path / ".cache" / COMMISSION_FILENAME, index=False)
    _status_frame(_status("1.100", "CLOSED", closed="2026-06-06T05:00:00Z")).to_csv(
        tmp_path / ".cache" / STATUS_FILENAME, index=False
    )

    report = build_daily_dm_report_from_results_dir(str(tmp_path), report_dt=REPORT_AT)

    assert report.day_to_date.total.commission == 0.88
    assert "• Total: gross $12.50, commission $0.88 (7.0%), net $11.62" in report.text


def test_unreadable_commission_store_degrades_to_unknown(tmp_path, caplog) -> None:
    (tmp_path / ".cache").mkdir()
    (tmp_path / "cleared_orders_cleaned.csv").write_text(
        "betId,marketId,eventTypeId,profit,settledDate\n"
        "1,1.100,7,12.5,2026-06-06T00:30:00Z\n",
        encoding="utf-8",
    )
    (tmp_path / ".cache" / COMMISSION_FILENAME).write_bytes(b"\xff\xfe\x00garbage")

    with caplog.at_level("WARNING"):
        report = build_daily_dm_report_from_results_dir(
            str(tmp_path), report_dt=REPORT_AT
        )

    assert report.day_to_date.total.unknown_markets == 1
    assert "Could not read market commission file" in caplog.text

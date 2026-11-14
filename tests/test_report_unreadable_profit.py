"""
A profit that does not parse must be announced, never a silent $0.00.

Covered at each layer: the schema flag on the shapes a ``dtype=str`` read
of the canonical really produces, the report figure and its line, and the
``dm-report`` command reading a CSV from disk.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from betfair_results_downloader.__main__ import main
from betfair_results_downloader.reporting.daily_dm_report import (
    build_daily_dm_report_from_dataframe,
)
from betfair_results_downloader.reporting.schema import normalize_cleared_orders_schema

SYDNEY_TZ = ZoneInfo("Australia/Sydney")
REPORT_DT = datetime(2026, 6, 6, 21, 0, tzinfo=SYDNEY_TZ)
SETTLED = "2026-06-06T00:30:00Z"


def _rows(profits: list[object]) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "betId": [str(n) for n in range(1, len(profits) + 1)],
            "eventTypeId": ["7"] * len(profits),
            "profit": profits,
            "settledDate": [SETTLED] * len(profits),
        }
    )


# -----------------------------
# Schema
# -----------------------------


@pytest.mark.parametrize(
    "raw,value,unreadable",
    [
        ("12.5", 12.5, False),
        ("-2.0", -2.0, False),
        ("0", 0.0, False),
        # A dtype=str read with keep_default_na=False gives "" for an empty cell.
        ("", 0.0, True),
        ("abc", 0.0, True),
        ("12,5", 0.0, True),
        (None, 0.0, True),
        (float("nan"), 0.0, True),
    ],
)
def test_the_schema_flags_a_profit_it_could_not_read(raw, value, unreadable) -> None:
    out = normalize_cleared_orders_schema(_rows([raw]))
    assert out["profit"].tolist() == [value]
    assert out["profit_unreadable"].tolist() == [unreadable]


def test_a_frame_with_no_profit_column_is_all_unreadable() -> None:
    frame = _rows(["1.0", "2.0"]).drop(columns="profit")
    out = normalize_cleared_orders_schema(frame)
    assert out["profit"].tolist() == [0.0, 0.0]
    assert out["profit_unreadable"].tolist() == [True, True]


# -----------------------------
# Report
# -----------------------------


def test_the_report_counts_unreadable_rows_and_says_so() -> None:
    report = build_daily_dm_report_from_dataframe(
        _rows(["10.0", "", "oops"]), report_dt=REPORT_DT
    )
    assert report.unreadable_profit_rows == 2
    assert report.day_to_date.total_profit == 10.0
    lines = report.text.splitlines()
    warning = (
        "⚠️ 2 bet rows have an unreadable profit (counted as $0.00); "
        "oldest settled 6 June 2026"
    )
    # Under the title and above the first section, where it is read first.
    assert lines.index(warning) < lines.index("*Today* (since 12:00 AM)")


def test_one_unreadable_row_reads_in_the_singular() -> None:
    report = build_daily_dm_report_from_dataframe(
        _rows(["10.0", ""]), report_dt=REPORT_DT
    )
    assert "⚠️ 1 bet row has an unreadable profit (counted as $0.00)" in report.text


def test_a_clean_canonical_carries_no_warning() -> None:
    report = build_daily_dm_report_from_dataframe(
        _rows(["10.0", "-3.5"]), report_dt=REPORT_DT
    )
    assert report.unreadable_profit_rows == 0
    assert report.unreadable_profit_oldest is None
    assert "unreadable profit" not in report.text


def test_the_warning_dates_a_damaged_row_outside_the_sections_shown() -> None:
    # One damaged row from February: every report carries the warning, and
    # none of the day or week sections contains the row, so the date is the
    # only clue to where it is.
    frame = _rows(["10.0", ""])
    frame.loc[1, "settledDate"] = "2026-02-03T01:00:00Z"
    report = build_daily_dm_report_from_dataframe(frame, report_dt=REPORT_DT)
    assert report.unreadable_profit_oldest.date().isoformat() == "2026-02-03"
    assert (
        "⚠️ 1 bet row has an unreadable profit (counted as $0.00); "
        "oldest settled 3 February 2026"
    ) in report.text


def test_rows_settled_after_the_report_time_are_not_counted() -> None:
    frame = _rows(["10.0", ""])
    frame.loc[1, "settledDate"] = "2026-06-07T00:30:00Z"  # after REPORT_DT
    report = build_daily_dm_report_from_dataframe(frame, report_dt=REPORT_DT)
    assert report.unreadable_profit_rows == 0


# -----------------------------
# CLI
# -----------------------------


def test_dm_report_reads_a_damaged_csv_and_prints_the_warning(tmp_path, capsys) -> None:
    csv_path = tmp_path / "cleared_orders_cleaned.csv"
    csv_path.write_text(
        "betId,eventTypeId,profit,settledDate\n"
        f"1,7,12.5,{SETTLED}\n"
        f"2,7,,{SETTLED}\n"
        f"3,4339,n/a,{SETTLED}\n",
        encoding="utf-8",
    )
    code = main(
        ["dm-report", "--csv", str(csv_path), "--at", "2026-06-06T21:00:00+10:00"]
    )
    out = capsys.readouterr().out
    assert code == 0
    assert "⚠️ 2 bet rows have an unreadable profit (counted as $0.00)" in out
    assert "• *Total: net $12.50*" in out

"""
Split settlements and the Azure profit aggregation source.

A market can settle across two download windows -- a partial cash-out in
one run, the remainder in the next. The Azure MarketProfit upsert is
keyed (UserID, MarketID), so aggregating only the window frame would
overwrite the market's profit with just the latest window's bets. The
aggregation source must be the canonical, restricted to the window's
marketIds.
"""

from __future__ import annotations

import inspect
from decimal import Decimal

import pandas as pd

from betfair_results_downloader.downloader_core import (
    prepare_azure_dataset,
    select_azure_rows_from_canonical,
)


def _row(market_id: str, bet_id: str, profit: str) -> dict:
    return {
        "marketId": market_id,
        "betId": bet_id,
        "profit": profit,
        "eventTypeId": "7",
        "placedDate": "2026-08-31T10:00:00.000Z",
    }


def test_split_settlement_aggregates_the_full_market() -> None:
    canonical = pd.DataFrame(
        [
            _row("1.234", "b1", "5.0"),
            _row("1.234", "b2", "-2.0"),
            _row("1.999", "b3", "10.0"),
        ],
        dtype=str,
    )
    window = pd.DataFrame([_row("1.234", "b2", "-2.0")], dtype=str)

    selected = select_azure_rows_from_canonical(canonical, window)

    assert len(selected) == 2, "both bets of the touched market"
    assert set(selected["marketId"]) == {"1.234"}, "untouched markets excluded"

    prep = prepare_azure_dataset(df_co=selected)
    assert prep.rows_to_write == [(Decimal("1.234"), Decimal("3.00"), "")]


def test_canonical_string_profits_sum_numerically() -> None:
    """The canonical is read dtype=str; string sums would concatenate."""
    df = pd.DataFrame(
        [_row("1.234", "b1", "5.0"), _row("1.234", "b2", "-2.0")], dtype=str
    )

    prep = prepare_azure_dataset(df_co=df)

    assert prep.rows_to_write == [(Decimal("1.234"), Decimal("3.00"), "")]


def test_missing_canonical_falls_back_to_the_window() -> None:
    """No canonical must mean old behaviour, never a skipped publish."""
    window = pd.DataFrame([_row("1.234", "b2", "-2.0")], dtype=str)

    assert select_azure_rows_from_canonical(None, window) is window
    empty = pd.DataFrame()
    assert select_azure_rows_from_canonical(empty, window) is window


def test_pipeline_aggregates_from_the_canonical() -> None:
    """The wiring, asserted at source level like the login-retry test."""
    from betfair_results_downloader.scheduler import runner

    source = inspect.getsource(runner._run_pipeline_inner)
    assert "select_azure_rows_from_canonical(csvr.df_canonical, df_co)" in source

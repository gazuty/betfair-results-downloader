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


def test_archived_out_markets_still_publish_from_the_window() -> None:
    """
    A backfill can download rows old enough that write_csv_outputs
    archives them straight out of the canonical. Their markets must
    still publish from the window rows, as they always did, rather than
    silently vanish from an empty selection.
    """
    canonical = pd.DataFrame([_row("1.111", "b1", "4.0")], dtype=str)
    window = pd.DataFrame(
        [_row("1.111", "b1", "4.0"), _row("1.222", "b9", "7.5")], dtype=str
    )

    selected = select_azure_rows_from_canonical(canonical, window)

    assert set(selected["marketId"]) == {"1.111", "1.222"}
    prep = prepare_azure_dataset(df_co=selected)
    profits = {str(m): p for m, p, _ in prep.rows_to_write}
    assert profits["1.222"] == Decimal("7.50")


def test_partially_archived_market_keeps_its_archived_bets() -> None:
    """
    Settlements straddling the archival cutoff: the canonical keeps the
    newer bet and archives the older one. The market is still present in
    the canonical, so a marketId-level fallback would miss the archived
    bet and understate the profit -- the supplement must work by betId.
    """
    canonical = pd.DataFrame([_row("1.234", "b2", "-2.0")], dtype=str)
    window = pd.DataFrame(
        [_row("1.234", "b1", "5.0"), _row("1.234", "b2", "-2.0")], dtype=str
    )

    selected = select_azure_rows_from_canonical(canonical, window)

    assert sorted(selected["betId"]) == ["b1", "b2"], "archived bet restored"
    prep = prepare_azure_dataset(df_co=selected)
    assert prep.rows_to_write == [(Decimal("1.234"), Decimal("3.00"), "")]


def test_unparseable_profit_aborts_preparation() -> None:
    """
    A profit that fails numeric coercion must abort, exactly as it
    aborted _money2 before -- pandas would otherwise skip the NaN and
    publish an understated market total as a success.
    """
    df = pd.DataFrame(
        [_row("1.234", "b1", "5.0"), _row("1.234", "b2", "garbage")], dtype=str
    )

    prep = prepare_azure_dataset(df_co=df)

    assert prep.attempted is False
    assert "unparseable" in prep.message
    assert prep.rows_to_write is None


def test_prep_failure_becomes_a_partial_run() -> None:
    """A failed preparation must never fall through to success."""
    from betfair_results_downloader.scheduler import runner

    source = inspect.getsource(runner._run_pipeline_inner)
    assert "if not prep.attempted:" in source
    assert source.index("if not prep.attempted:") < source.index(
        "if prep.rows_to_write:"
    )


def test_malformed_profit_in_excluded_sport_does_not_block_racing() -> None:
    """
    Downloads include every sport; Azure publishes only racing. A garbage
    profit on a soccer row must not stop the racing markets publishing.
    """
    soccer = _row("1.555", "b9", "garbage")
    soccer["eventTypeId"] = "1"  # soccer -- excluded from Azure
    df = pd.DataFrame([_row("1.234", "b1", "5.0"), soccer], dtype=str)

    prep = prepare_azure_dataset(df_co=df)

    assert prep.attempted is True
    assert prep.rows_to_write == [(Decimal("1.234"), Decimal("5.00"), "")]


def test_float_damaged_market_spellings_select_together() -> None:
    """
    8.6% of historical rows carry float-damaged marketIds (1.2515001 for
    1.251500100). Numerically -- and in Azure's Decimal key -- they are
    one market, so selection and grouping must treat them as one.
    """
    canonical = pd.DataFrame(
        [_row("1.2515001", "b1", "5.0"), _row("1.999", "b3", "9.0")], dtype=str
    )
    window = pd.DataFrame([_row("1.251500100", "b2", "-2.0")], dtype=str)

    selected = select_azure_rows_from_canonical(canonical, window)

    assert sorted(selected["betId"]) == ["b1", "b2"], "damaged spelling included"
    prep = prepare_azure_dataset(df_co=selected)
    assert prep.rows_to_write == [(Decimal("1.2515001"), Decimal("3.00"), "")]


def test_legacy_float_betids_do_not_double_count() -> None:
    """
    The canonical dedupe treats legacy "123.0" and fresh 123 as one bet
    (betid_keys); the supplement must use the same normalisation or the
    window copy of an already-held bet would be counted twice.
    """
    canonical = pd.DataFrame([_row("1.234", "123.0", "5.0")], dtype=str)
    window = pd.DataFrame([_row("1.234", "123", "5.0")], dtype=str)

    selected = select_azure_rows_from_canonical(canonical, window)

    assert len(selected) == 1, "same bet, one row"
    prep = prepare_azure_dataset(df_co=selected)
    assert prep.rows_to_write == [(Decimal("1.234"), Decimal("5.00"), "")]


def test_distinct_keyless_bets_are_both_kept() -> None:
    """
    Two different bets can both carry an unusable betId (empty string).
    clean_and_remove_duplicates preserves such rows by full-row identity,
    so the supplement must not collapse them onto a shared empty key --
    that would drop an archived keyless bet and understate the market.
    """
    kept = _row("1.234", "", "5.0")
    archived = _row("1.234", "", "-2.0")  # distinct bet, same unusable id
    canonical = pd.DataFrame([kept], dtype=str)
    window = pd.DataFrame([kept, archived], dtype=str)

    selected = select_azure_rows_from_canonical(canonical, window)

    assert len(selected) == 2, "identical row deduped, distinct row kept"
    prep = prepare_azure_dataset(df_co=selected)
    assert prep.rows_to_write == [(Decimal("1.234"), Decimal("3.00"), "")]

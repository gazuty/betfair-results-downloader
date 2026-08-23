"""
Regression tests for the dedupe sort-order bug: settledDate/placedDate
round-trip through CSV as strings, and different renderings of the same
instant ("2026-07-13T04:58:46Z" vs "2026-07-13 04:58:46+00:00") used to be
sorted lexicographically. That could place a stale existing row after the
fresh incoming row, so drop_duplicates(keep="last") kept the stale row and
silently discarded enrichment. Dedupe now sorts on typed keys, where equal
instants tie and the stable sort preserves input order (existing before
incoming), so incoming data wins.
"""

import pandas as pd

from betfair_results_downloader.csv_utils import (
    clean_and_remove_duplicates,
    update_csv_with_new_data,
)


def test_incoming_row_wins_tie_regardless_of_timestamp_rendering():
    # Same instant, two renderings. Lexicographically "2026-07-13T..." sorts
    # AFTER "2026-07-13 ..." (T > space), so a raw string sort would move the
    # stale first row last and keep it. Typed keys must keep the incoming row.
    df = pd.DataFrame(
        {
            "betId": ["111", "111"],
            "settledDate": ["2026-07-13T04:58:46Z", "2026-07-13 04:58:46+00:00"],
            "runner_name": [None, "Fast Horse"],
        }
    )

    result = clean_and_remove_duplicates(df)

    assert len(result) == 1
    assert result["runner_name"].iloc[0] == "Fast Horse"
    # temporary sort keys must not leak into the output
    assert not [c for c in result.columns if c.startswith("_sort_")]


def test_update_csv_prefers_fresh_enriched_row(tmp_path):
    path = tmp_path / "canonical.csv"
    existing = pd.DataFrame(
        {
            "betId": [111],
            "settledDate": ["2026-07-13T04:58:46Z"],
            "runner_name": [None],
        }
    )
    existing.to_csv(path, index=False)

    incoming = pd.DataFrame(
        {
            "betId": [111],
            "settledDate": ["2026-07-13 04:58:46+00:00"],
            "runner_name": ["Fast Horse"],
        }
    )
    update_csv_with_new_data(path, incoming)

    result = pd.read_csv(path)
    assert len(result) == 1
    assert result["runner_name"].iloc[0] == "Fast Horse"


def test_mixed_type_sort_columns_do_not_crash():
    # Canonical CSVs load with mixed types in these columns (DtypeWarning in
    # prod logs); unparseable values must coerce to NaT/NaN, not raise.
    df = pd.DataFrame(
        {
            "betId": ["1", "2", "3"],
            "settledDate": ["2026-07-13T04:58:46Z", float("nan"), "not a date"],
            "marketId": ["1.23", float("nan"), "abc"],
        }
    )

    result = clean_and_remove_duplicates(df)

    assert len(result) == 3
    assert not [c for c in result.columns if c.startswith("_sort_")]

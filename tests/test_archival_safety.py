"""
Archival deletes rows from the canonical and keeps the only remaining copy.

This path has never executed on the live file -- the canonical starts
2025-12-11 against a 12-month cutoff -- so its first run will be on the
oldest and least reproducible data there is.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from betfair_results_downloader.csv_utils import (
    clean_and_remove_duplicates,
    update_csv_with_new_data,
)
from betfair_results_downloader.downloader_core import archive_old_canonical_rows

from conftest import make_cleared_orders


def _old(days: int = 500) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).strftime(
        "%Y-%m-%dT%H:%M:%S.000Z"
    )


def test_rows_without_a_betid_are_not_collapsed() -> None:
    """
    pandas treats NaN as equal to NaN in drop_duplicates, so unparseable
    betIds used to collapse into a single row. In memory that is merely
    wrong; on the archival path the originals are then deleted.
    """
    df = pd.DataFrame(
        {
            "betId": ["", "", "", "123"],
            "profit": [1.0, 2.0, 3.0, 4.0],
            "settledDate": [_old(), _old(), _old(), _old()],
        }
    )

    result = clean_and_remove_duplicates(df)

    assert len(result) == 4, "three distinct keyless rows must all survive"
    assert sorted(result["profit"].astype(float)) == [1.0, 2.0, 3.0, 4.0]


def test_identical_keyless_rows_still_dedupe() -> None:
    """Keyless rows are deduped on full-row equality, not kept blindly."""
    df = pd.DataFrame(
        {
            "betId": ["", "", "123"],
            "profit": [1.0, 1.0, 2.0],
            "settledDate": [_old(), _old(), _old()],
        }
    )

    result = clean_and_remove_duplicates(df)

    assert len(result) == 2


def test_archival_preserves_every_keyless_row(results_dir) -> None:
    """The end-to-end version: nothing is deleted that was not archived."""
    df = make_cleared_orders(20)
    df["settledDate"] = _old()
    df["betId"] = df["betId"].astype(str)
    df.loc[df.index[:5], "betId"] = ""

    trimmed = archive_old_canonical_rows(df, results_dir, archive_months=12)

    assert len(trimmed) == 0, "all rows are past the cutoff"
    year = (datetime.now(timezone.utc) - timedelta(days=500)).year
    archive = results_dir / f"cleared_orders_archive_{year}.csv.gz"
    archived = pd.read_csv(archive, dtype=str, keep_default_na=False)
    assert len(archived) == 20, "every row removed must appear in the archive"


def test_archival_refuses_to_delete_more_than_it_stores(
    results_dir, monkeypatch
) -> None:
    """
    If the archive does not grow by at least the number of rows leaving the
    canonical, the run must fail rather than silently lose them.
    """
    df = make_cleared_orders(10)
    df["settledDate"] = _old()

    # Simulate any dedupe fault that swallows rows on the way in.
    monkeypatch.setattr(
        "betfair_results_downloader.downloader_core.clean_and_remove_duplicates",
        lambda frame, **_kw: frame.head(3),
    )

    with pytest.raises(ValueError, match="absent from the archive"):
        archive_old_canonical_rows(df, results_dir, archive_months=12)


def test_archive_preserves_market_ids(results_dir) -> None:
    """The archive is the last copy; it must not truncate IDs either."""
    df = make_cleared_orders(30)
    df["settledDate"] = _old()
    expected = set(df["marketId"].astype(str))

    archive_old_canonical_rows(df, results_dir, archive_months=12)
    # Second pass re-reads the archive and rewrites it.
    df2 = make_cleared_orders(10, first_bet_id=500, seed=5)
    df2["settledDate"] = _old()
    archive_old_canonical_rows(df2, results_dir, archive_months=12)

    year = (datetime.now(timezone.utc) - timedelta(days=500)).year
    archive = results_dir / f"cleared_orders_archive_{year}.csv.gz"
    archived = pd.read_csv(archive, dtype=str, keep_default_na=False)

    assert expected.issubset(set(archived["marketId"].astype(str)))


def test_merge_refuses_to_shrink_the_canonical(tmp_path, monkeypatch) -> None:
    """
    A merge adds rows or leaves them alone. A drop means a dedupe fault, and
    writing it would atomically replace the system of record with the
    damaged version.
    """
    path = tmp_path / "cleared_orders_cleaned.csv"
    update_csv_with_new_data(path, make_cleared_orders(100))
    before = path.read_bytes()

    calls = {"n": 0}

    def swallow(frame, **_kw):
        calls["n"] += 1
        # Let the incoming frame through, damage the merged result.
        return frame if calls["n"] == 1 else frame.head(5)

    monkeypatch.setattr(
        "betfair_results_downloader.csv_utils.clean_and_remove_duplicates", swallow
    )

    with pytest.raises(ValueError, match="absent after merging"):
        update_csv_with_new_data(path, make_cleared_orders(10, first_bet_id=900))

    assert path.read_bytes() == before, "the existing file must be untouched"


def test_existing_duplicates_can_still_be_cleaned_up(tmp_path) -> None:
    """
    A canonical that already contains duplicate betIds legitimately shrinks
    when merged and deduped. Guarding on row count would abort every run from
    then on and leave the file unrepairable through the normal path.
    """
    path = tmp_path / "cleared_orders_cleaned.csv"
    df = make_cleared_orders(50)
    doubled = pd.concat([df, df], ignore_index=True)
    doubled.to_csv(path, index=False)
    assert len(pd.read_csv(path)) == 100

    _, combined = update_csv_with_new_data(
        path, make_cleared_orders(10, first_bet_id=900, seed=4)
    )

    assert len(combined) == 60, "50 deduped + 10 new"
    on_disk = pd.read_csv(path)
    assert len(on_disk) == 60


def test_legacy_float_betids_are_not_reported_as_lost(tmp_path) -> None:
    """
    Dedupe compares betIds numerically, so a legacy "123.0" in the file and a
    fresh 123 from the API are one record. A guard comparing raw strings would
    call the first lost and abort every run that overlapped it.
    """
    path = tmp_path / "cleared_orders_cleaned.csv"
    existing = make_cleared_orders(20)
    existing["betId"] = existing["betId"].astype(float)  # writes as "1.0", "2.0", ...
    existing.to_csv(path, index=False)

    incoming = make_cleared_orders(20)  # same ids, integer form

    _, combined = update_csv_with_new_data(path, incoming)

    assert len(combined) == 20, "the same records, not 40"


def test_betid_keys_normalises_like_the_dedupe_key() -> None:
    from betfair_results_downloader.csv_utils import betid_keys

    assert betid_keys(pd.DataFrame({"betId": ["123.0"]})) == betid_keys(
        pd.DataFrame({"betId": [123]})
    )
    assert betid_keys(pd.DataFrame({"betId": ["", "nope", None]})) == set()
    assert betid_keys(pd.DataFrame({"profit": [1.0]})) == set()

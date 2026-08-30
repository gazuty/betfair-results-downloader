"""
The canonical CSV round-trip, at a realistic shape.

Everything else in this suite exercises the CSV path at one to three rows and
three or four columns. The interactions that can destroy the real ~1M-row
file -- schema union and reindex, dedupe across a merge, the snapshot written
from the trimmed frame -- only appear once the frame has the real 39 columns
and mixed dtypes, and only once the pipeline has run over the same file more
than once.
"""

from __future__ import annotations

import pandas as pd
import pytest

from betfair_results_downloader.downloader_core import write_csv_outputs

from conftest import CANONICAL_COLUMNS, make_cleared_orders


def _canonical(results_dir):
    return results_dir / "cleared_orders_cleaned.csv"


def _write(df, results_dir):
    """
    archive_months=0 disables archival deliberately.

    These tests are about the merge, not the archive, and the fixture uses
    fixed 2026 dates: with the 12-month default they would start moving rows
    to archive files once the wall clock passed June 2027 and fail with no
    product regression. Archival has its own tests.
    """
    return write_csv_outputs(df_co=df, results_csv_dir=results_dir, archive_months=0)


def test_first_write_preserves_every_row_and_column(results_dir) -> None:
    df = make_cleared_orders(500)

    _write(df, results_dir)

    written = pd.read_csv(_canonical(results_dir), dtype=str, keep_default_na=False)
    assert len(written) == 500
    assert set(written.columns) == set(CANONICAL_COLUMNS)


def test_second_run_merges_without_losing_rows(results_dir) -> None:
    """
    Two runs in sequence, with the overlap the 2h re-download window creates.
    A dedupe regression here silently rewrites the system of record.
    """
    first = make_cleared_orders(500, first_bet_id=1)
    _write(first, results_dir)

    # 100 rows of overlap, 400 genuinely new -- the real shape of a run.
    second = make_cleared_orders(500, first_bet_id=401, seed=99)
    _write(second, results_dir)

    written = pd.read_csv(_canonical(results_dir), dtype=str, keep_default_na=False)
    ids = written["betId"].astype(int)

    assert len(written) == 900, "500 + 500 with 100 overlapping"
    assert ids.is_unique, "the merge must not duplicate a betId"
    assert set(ids) == set(range(1, 901))


def test_repeated_identical_run_is_idempotent(results_dir) -> None:
    df = make_cleared_orders(200)

    _write(df, results_dir)
    first = _canonical(results_dir).read_text(encoding="utf-8")
    _write(df, results_dir)
    second = _canonical(results_dir).read_text(encoding="utf-8")

    assert first == second, "re-running the same window must change nothing"


def test_snapshot_matches_the_canonical(results_dir) -> None:
    df = make_cleared_orders(300)

    result = _write(df, results_dir)

    canonical = pd.read_csv(_canonical(results_dir), dtype=str, keep_default_na=False)
    snapshot = pd.read_csv(result.snapshot_path, dtype=str, keep_default_na=False)

    assert len(snapshot) == len(canonical)
    assert set(snapshot.columns) == set(canonical.columns)


@pytest.mark.xfail(
    strict=True,
    reason=(
        "Known defect, same root cause as the marketId truncation: "
        "write_csv_outputs re-reads the canonical with inferred dtypes before "
        "writing the snapshot, so numeric-looking string columns diverge "
        "(12345678 in the canonical, 12345678.0 in the snapshot) on the very "
        "first write. The snapshot is the only backup of the canonical, so "
        "they should be identical. Fixed by the dtype PR."
    ),
)
def test_snapshot_values_match_the_canonical(results_dir) -> None:
    """
    Shape alone would pass even if every value differed -- so compare
    contents. The snapshot is the recovery copy; a snapshot that does not
    match the file it snapshots is not a backup.
    """
    df = make_cleared_orders(300)

    result = _write(df, results_dir)

    canonical = pd.read_csv(_canonical(results_dir), dtype=str, keep_default_na=False)
    snapshot = pd.read_csv(result.snapshot_path, dtype=str, keep_default_na=False)

    cols = sorted(canonical.columns)
    left = canonical[cols].sort_values("betId").reset_index(drop=True)
    right = snapshot[cols].sort_values("betId").reset_index(drop=True)
    pd.testing.assert_frame_equal(left, right)


def test_numeric_columns_survive_the_round_trip(results_dir) -> None:
    df = make_cleared_orders(200)
    expected = df.set_index("betId")["profit"].round(2).to_dict()

    _write(df, results_dir)
    write_csv_outputs(
        df_co=make_cleared_orders(50, first_bet_id=500, seed=7),
        results_csv_dir=results_dir,
    )

    written = pd.read_csv(_canonical(results_dir))
    got = written.set_index("betId")["profit"].round(2).to_dict()

    for bet_id, profit in expected.items():
        assert got[bet_id] == pytest.approx(profit), f"profit drifted for {bet_id}"


@pytest.mark.xfail(
    strict=True,
    reason=(
        "Known defect: the canonical is read without dtype=, so pandas infers "
        "float64 for marketId and strips trailing digits. Measured at 8.6% of "
        "rows on the live file. Fixed in the dtype PR, which flips this to a "
        "passing assertion."
    ),
)
def test_market_ids_are_not_truncated_by_the_round_trip(results_dir) -> None:
    """
    Betfair market IDs are '1.' plus nine digits. A float round-trip destroys
    any trailing zero, which breaks string joins against the catalogue cache.
    """
    df = make_cleared_orders(500)
    expected = dict(zip(df["betId"].astype(str), df["marketId"].astype(str)))

    _write(df, results_dir)
    # A second run re-reads and rewrites every existing row.
    write_csv_outputs(
        df_co=make_cleared_orders(50, first_bet_id=900, seed=3),
        results_csv_dir=results_dir,
    )

    written = pd.read_csv(_canonical(results_dir), dtype=str, keep_default_na=False)
    got = dict(zip(written["betId"].astype(str), written["marketId"].astype(str)))

    damaged = {k: (v, got[k]) for k, v in expected.items() if got.get(k) != v}
    assert not damaged, (
        f"{len(damaged)} market IDs changed, e.g. {list(damaged.items())[:3]}"
    )

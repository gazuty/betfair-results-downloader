from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from betfair_results_downloader.downloader_core import (
    archive_old_canonical_rows,
    write_csv_outputs,
)


def _df(rows: list[tuple[int, str]]) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "betId": [r[0] for r in rows],
            "settledDate": [r[1] for r in rows],
            "profit": [1.0] * len(rows),
        }
    )


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


NOW = datetime.now(timezone.utc)
OLD_2024 = "2024-03-15T10:00:00+00:00"
OLD_2025 = "2025-01-20T10:00:00+00:00"
RECENT = _iso(NOW)


def test_archive_moves_old_rows_to_yearly_gz(tmp_path: Path):
    df = _df([(1, OLD_2024), (2, OLD_2025), (3, RECENT)])

    trimmed = archive_old_canonical_rows(df, tmp_path, archive_months=12)

    assert trimmed["betId"].tolist() == [3]
    arch_2024 = pd.read_csv(tmp_path / "cleared_orders_archive_2024.csv.gz")
    arch_2025 = pd.read_csv(tmp_path / "cleared_orders_archive_2025.csv.gz")
    assert arch_2024["betId"].tolist() == [1]
    assert arch_2025["betId"].tolist() == [2]


def test_archive_appends_and_dedupes(tmp_path: Path):
    df = _df([(1, OLD_2024), (3, RECENT)])
    archive_old_canonical_rows(df, tmp_path, archive_months=12)

    # Re-run with the same old row plus a new old row: no duplicates
    df2 = _df([(1, OLD_2024), (2, OLD_2024), (3, RECENT)])
    trimmed = archive_old_canonical_rows(df2, tmp_path, archive_months=12)

    assert trimmed["betId"].tolist() == [3]
    arch = pd.read_csv(tmp_path / "cleared_orders_archive_2024.csv.gz")
    assert sorted(arch["betId"].tolist()) == [1, 2]


def test_archive_keeps_unparseable_settled_dates(tmp_path: Path):
    df = _df([(1, "not-a-date"), (2, RECENT)])

    trimmed = archive_old_canonical_rows(df, tmp_path, archive_months=12)

    assert sorted(trimmed["betId"].tolist()) == [1, 2]
    assert not list(tmp_path.glob("cleared_orders_archive_*"))


def test_archive_disabled_with_nonpositive_months(tmp_path: Path):
    df = _df([(1, OLD_2024), (2, RECENT)])

    trimmed = archive_old_canonical_rows(df, tmp_path, archive_months=0)

    assert len(trimmed) == 2
    assert not list(tmp_path.glob("cleared_orders_archive_*"))


def test_write_csv_outputs_archives_and_trims_canonical(tmp_path: Path):
    df = _df([(1, OLD_2024), (2, RECENT)])

    result = write_csv_outputs(
        df_co=df,
        results_csv_dir=tmp_path,
        status_cb=None,
        archive_months=12,
    )

    assert result.rows_in_canonical == 1
    canonical = pd.read_csv(tmp_path / "cleared_orders_cleaned.csv")
    assert canonical["betId"].tolist() == [2]
    arch = pd.read_csv(tmp_path / "cleared_orders_archive_2024.csv.gz")
    assert arch["betId"].tolist() == [1]
    snapshot = pd.read_csv(result.snapshot_path)
    assert snapshot["betId"].tolist() == [2]

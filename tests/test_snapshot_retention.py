from pathlib import Path

import pandas as pd

from betfair_results_downloader.downloader_core import (
    prune_snapshot_files,
    write_csv_outputs,
)


def _make_snapshots(results_dir: Path, dates: list[str], suffix: str = ".csv") -> None:
    for d in dates:
        (results_dir / f"cleared_orders_cleaned_{d}{suffix}").write_text("betId\n1\n")


def test_prune_keeps_newest_and_deletes_older(tmp_path: Path):
    dates = [f"2026-06-{day:02d}" for day in range(1, 21)]
    _make_snapshots(tmp_path, dates)

    deleted = prune_snapshot_files(tmp_path, keep=14)

    remaining = sorted(p.name for p in tmp_path.glob("cleared_orders_cleaned_*.csv"))
    assert len(deleted) == 6
    assert len(remaining) == 14
    assert remaining[0] == "cleared_orders_cleaned_2026-06-07.csv"
    assert remaining[-1] == "cleared_orders_cleaned_2026-06-20.csv"


def test_prune_never_touches_canonical_or_unrelated_files(tmp_path: Path):
    canonical = tmp_path / "cleared_orders_cleaned.csv"
    canonical.write_text("betId\n1\n")
    unrelated = tmp_path / "cleared_orders_backup_2026-01-01.csv"
    unrelated.write_text("betId\n1\n")
    _make_snapshots(tmp_path, [f"2026-06-{day:02d}" for day in range(1, 6)])

    deleted = prune_snapshot_files(tmp_path, keep=2)

    assert canonical.exists()
    assert unrelated.exists()
    assert len(deleted) == 3


def test_prune_handles_mixed_csv_and_gz(tmp_path: Path):
    _make_snapshots(tmp_path, ["2026-06-01", "2026-06-02"], suffix=".csv")
    _make_snapshots(tmp_path, ["2026-06-03", "2026-06-04"], suffix=".csv.gz")

    prune_snapshot_files(tmp_path, keep=2)

    remaining = sorted(p.name for p in tmp_path.glob("cleared_orders_cleaned_*"))
    assert remaining == [
        "cleared_orders_cleaned_2026-06-03.csv.gz",
        "cleared_orders_cleaned_2026-06-04.csv.gz",
    ]


def test_prune_disabled_when_keep_nonpositive(tmp_path: Path):
    _make_snapshots(tmp_path, ["2026-06-01", "2026-06-02", "2026-06-03"])

    assert prune_snapshot_files(tmp_path, keep=0) == []
    assert len(list(tmp_path.glob("cleared_orders_cleaned_*.csv"))) == 3


def test_write_csv_outputs_writes_gz_snapshot_and_prunes(tmp_path: Path):
    _make_snapshots(tmp_path, [f"2026-05-{day:02d}" for day in range(1, 21)])

    df = pd.DataFrame({"betId": [1], "profit": [2.5]})
    result = write_csv_outputs(
        df_co=df,
        results_csv_dir=tmp_path,
        status_cb=None,
        snapshot_retention=5,
        compress_snapshots=True,
    )

    assert result.snapshot_path.name.endswith(".csv.gz")
    assert result.snapshot_path.exists()
    snapshot_df = pd.read_csv(result.snapshot_path)
    assert snapshot_df["betId"].iloc[0] == 1

    snapshots = [
        p for p in tmp_path.iterdir() if p.name.startswith("cleared_orders_cleaned_")
    ]
    assert len(snapshots) == 5


def test_write_csv_outputs_uncompressed_snapshot(tmp_path: Path):
    df = pd.DataFrame({"betId": [1], "profit": [2.5]})
    result = write_csv_outputs(
        df_co=df,
        results_csv_dir=tmp_path,
        status_cb=None,
        compress_snapshots=False,
    )

    assert result.snapshot_path.name.endswith(".csv")
    assert not result.snapshot_path.name.endswith(".csv.gz")
    assert result.snapshot_path.exists()

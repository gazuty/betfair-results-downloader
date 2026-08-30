import csv
from datetime import datetime, timezone
from pathlib import Path

from betfair_results_downloader.audit import compute_missing_settled_dates


def test_audit_missing_dates_simple(tmp_path: Path) -> None:
    csv_path = tmp_path / "cleared_orders_cleaned.csv"
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["settledDate"])
        writer.writeheader()
        writer.writerow({"settledDate": "2026-01-01T00:00:00Z"})
        writer.writerow({"settledDate": "2026-01-03T00:00:00Z"})

    # now_utc pinned to the last day with data. Previously this passed None
    # and relied on the real clock: the audit clamped its scan to the newest
    # row, so "today" never mattered. It does now, and a test whose result
    # changes with the wall clock is not a test.
    summary = compute_missing_settled_dates(
        csv_path,
        max_ranges=10,
        window_days=None,
        now_utc=datetime(2026, 1, 3, 12, 0, tzinfo=timezone.utc),
    )

    assert summary["earliest"] == "2026-01-01"
    assert summary["latest"] == "2026-01-03"
    assert summary["num_missing"] == 1
    assert summary["missing_ranges"] == [
        {"start": "2026-01-02", "end": "2026-01-02", "days": 1}
    ]


def test_audit_missing_dates_real_gap_jan_2026(tmp_path: Path) -> None:
    csv_path = tmp_path / "cleared_orders_cleaned.csv"
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["settledDate"])
        writer.writeheader()
        writer.writerow({"settledDate": "2026-01-19T00:00:00Z"})
        writer.writerow({"settledDate": "2026-01-25T00:00:00Z"})
        writer.writerow({"settledDate": "2026-01-26T00:00:00Z"})

    now_utc = datetime(2026, 1, 26, 12, 0, tzinfo=timezone.utc)
    summary = compute_missing_settled_dates(
        csv_path,
        window_days=90,
        now_utc=now_utc,
    )

    assert summary["num_missing"] == 5
    assert summary["missing_ranges"] == [
        {"start": "2026-01-20", "end": "2026-01-24", "days": 5}
    ]
    assert summary["latest"] == "2026-01-26"
    assert summary["today"] == "2026-01-26"

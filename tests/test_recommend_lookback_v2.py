from datetime import datetime, timezone
import csv
from pathlib import Path

from betfair_results_downloader.recommend import recommend_lookback_days_v2
from betfair_results_downloader.state import save_run_state


def _write_settled_dates(csv_path: Path, dates: list[str]) -> None:
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["settledDate"])
        writer.writeheader()
        for d in dates:
            writer.writerow({"settledDate": f"{d}T00:00:00Z"})


def test_v2_missing_gap_within_window_triggers_missing_dates(tmp_path: Path) -> None:
    canonical = tmp_path / "cleared_orders_cleaned.csv"
    _write_settled_dates(
        canonical,
        ["2026-01-19", "2026-01-25", "2026-01-26"],
    )
    run_state = tmp_path / "run_state.json"
    save_run_state(run_state, {"last_success_utc": "2026-01-10T00:00:00Z"})

    now_utc = datetime(2026, 1, 26, 12, 0, tzinfo=timezone.utc)
    rec = recommend_lookback_days_v2(
        canonical_csv_path=canonical,
        run_state_path=run_state,
        now_utc=now_utc,
        window_days=90,
    )

    assert rec["lookback_source"] == "missing_dates"
    assert rec["recommended_days"] == 8
    assert rec["missing_range"]["start"] == "2026-01-20"
    assert rec["missing_range"]["end"] == "2026-01-24"


def test_v2_old_date_outside_90d_window_does_not_trigger_missing_dates(
    tmp_path: Path,
) -> None:
    canonical = tmp_path / "cleared_orders_cleaned.csv"
    _write_settled_dates(
        canonical,
        ["2025-01-01", "2026-01-25", "2026-01-26"],
    )
    run_state = tmp_path / "run_state.json"
    save_run_state(run_state, {"last_success_utc": "2026-01-25T00:00:00Z"})

    now_utc = datetime(2026, 1, 26, 12, 0, tzinfo=timezone.utc)
    rec = recommend_lookback_days_v2(
        canonical_csv_path=canonical,
        run_state_path=run_state,
        now_utc=now_utc,
        window_days=90,
    )

    assert rec["lookback_source"] == "run_state"


def test_v2_realistic_missing_gap_prefers_missing_dates(tmp_path: Path) -> None:
    canonical = tmp_path / "cleared_orders_cleaned.csv"
    _write_settled_dates(
        canonical,
        ["2026-01-19", "2026-01-25", "2026-01-26"],
    )
    run_state = tmp_path / "run_state.json"
    save_run_state(run_state, {"last_success_utc": "2026-01-20T00:00:00Z"})

    now_utc = datetime(2026, 1, 26, 12, 0, tzinfo=timezone.utc)
    rec = recommend_lookback_days_v2(
        canonical_csv_path=canonical,
        run_state_path=run_state,
        now_utc=now_utc,
        window_days=90,
    )

    assert rec["lookback_source"] == "missing_dates"
    assert rec["missing_range"] == {
        "start": "2026-01-20",
        "end": "2026-01-24",
        "days": 5,
    }
    assert rec["recommended_days"] == 8


def test_audit_window_bounds_are_respected(tmp_path: Path) -> None:
    canonical = tmp_path / "cleared_orders_cleaned.csv"
    _write_settled_dates(
        canonical,
        ["2026-01-04", "2026-01-06"],
    )
    run_state = tmp_path / "run_state.json"

    now_utc = datetime(2026, 1, 6, 12, 0, tzinfo=timezone.utc)
    rec = recommend_lookback_days_v2(
        canonical_csv_path=canonical,
        run_state_path=run_state,
        now_utc=now_utc,
        window_days=2,
    )

    assert rec["lookback_source"] == "missing_dates"
    assert rec["missing_range"] == {
        "start": "2026-01-05",
        "end": "2026-01-05",
        "days": 1,
    }
    assert rec["recommended_days"] == 3


def test_v2_first_run_default_when_no_data(tmp_path: Path) -> None:
    canonical = tmp_path / "cleared_orders_cleaned.csv"
    run_state = tmp_path / "run_state.json"

    now_utc = datetime(2026, 1, 26, 12, 0, tzinfo=timezone.utc)
    rec = recommend_lookback_days_v2(
        canonical_csv_path=canonical,
        run_state_path=run_state,
        now_utc=now_utc,
        window_days=90,
    )

    assert rec["lookback_source"] == "first_run_default"
    assert rec["recommended_days"] == 90

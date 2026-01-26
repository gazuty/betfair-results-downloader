from datetime import date, datetime, timedelta, timezone

from betfair_results_downloader.recommend import (
    compute_days_to_download,
    recommend_lookback_days,
)
from betfair_results_downloader.state import save_run_state


def test_compute_days_to_download_timezone_aware() -> None:
    last_settled = datetime(2026, 1, 20, 23, 30, tzinfo=timezone(timedelta(hours=11)))
    now_utc = datetime(2026, 1, 26, 1, 0, tzinfo=timezone.utc)

    assert compute_days_to_download(last_settled, now_utc) == 7


def test_compute_days_to_download_five_day_gap() -> None:
    last_settled = datetime(2026, 1, 21, 5, 0, tzinfo=timezone.utc)
    now_utc = datetime(2026, 1, 26, 8, 0, tzinfo=timezone.utc)

    assert compute_days_to_download(last_settled, now_utc) == 6


def test_recommend_lookback_prefers_run_state(tmp_path) -> None:
    run_state_path = tmp_path / "run_state.json"
    save_run_state(
        run_state_path,
        {
            "last_success_utc": "2026-01-20T00:00:00Z",
        },
    )

    now_utc = datetime(2026, 1, 26, 0, 0, tzinfo=timezone.utc)
    days, note, last_date, source = recommend_lookback_days(
        tmp_path,
        status_cb=None,
        now_utc=now_utc,
    )

    assert days == 7
    assert last_date == date(2026, 1, 20)
    assert source == "run_state"
    assert "Run state found" in note

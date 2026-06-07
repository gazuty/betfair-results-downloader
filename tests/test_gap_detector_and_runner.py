"""Tests for scheduler gap detection and intraday runner semantics."""
from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pandas as pd

from betfair_results_downloader.config import ScheduleConfig, parse_schedule_config
from betfair_results_downloader.scheduler.gap_detector import _max_settled_date_from_csv, compute_backfill_window
from betfair_results_downloader.scheduler.runner import _resolve_results_dir, run_scheduled
from betfair_results_downloader.scheduler.state import ScheduleStateRow
from betfair_results_downloader.scheduler.time_semantics import get_scheduler_now

BASE_CREDS = {
    "betfair": {
        "username": "u",
        "password": "p",
        "app_key": "k",
        "certs_dir": "/fake/certs",
    },
    "user": {
        "user_id": "TestUser",
        "db_user_id": "TestUser",
        "enable_azure_sql": False,
        "dry_run": True,
    },
    "paths": {"results_csv_dir": ""},
    "azure_sql": {
        "server": "srv.database.windows.net",
        "port": 1433,
        "database": "DB",
        "username": "adm",
        "password": "pw",
        "driver": "ODBC Driver 18 for SQL Server",
    },
    "schedule": {
        "enabled": True,
        "timezone": "Australia/Sydney",
        "primary_time": "05:30",
        "retry_times": [],
        "publish_to_azure": True,
        "allow_azure_publish": False,
        "max_backfill_days": 90,
        "chunk_days": 30,
        "min_coverage_overlap_days": 1,
        "min_overlap_hours": 2,
        "log_dir": "",
    },
}


def _default_schedule_cfg(**overrides):
    cfg = parse_schedule_config(BASE_CREDS)
    if not overrides:
        return cfg
    data = {
        "enabled": cfg.enabled,
        "timezone": cfg.timezone,
        "primary_time": cfg.primary_time,
        "retry_times": cfg.retry_times,
        "publish_to_azure": cfg.publish_to_azure,
        "allow_azure_publish": cfg.allow_azure_publish,
        "max_backfill_days": cfg.max_backfill_days,
        "chunk_days": cfg.chunk_days,
        "min_coverage_overlap_days": cfg.min_coverage_overlap_days,
        "min_overlap_hours": cfg.min_overlap_hours,
        "log_dir": cfg.log_dir,
        "history_file": cfg.history_file,
    }
    data.update(overrides)
    return ScheduleConfig(**data)


class TestComputeBackfillWindow:
    def test_uses_azure_confirmed_timestamp_when_available(self) -> None:
        checkpoint = datetime(2026, 6, 6, 17, 15, tzinfo=timezone.utc)
        azure_row = ScheduleStateRow(
            user_id="TestUser",
            last_covered_date_utc=date(2026, 6, 6),
            last_covered_date_local=date(2026, 6, 7),
            last_covered_timezone="Australia/Sydney",
            last_confirmed_settled_at_utc=checkpoint,
            last_successful_download_started_utc=None,
            last_successful_download_finished_utc=None,
            last_run_started_utc=None,
            last_run_finished_utc=None,
            last_run_status="success",
            last_run_message=None,
            updated_utc=None,
        )
        cfg = _default_schedule_cfg(min_overlap_hours=2)
        fake_now = get_scheduler_now(cfg, datetime(2026, 6, 6, 19, 30, tzinfo=timezone.utc))
        with patch("betfair_results_downloader.scheduler.gap_detector.get_scheduler_now", return_value=fake_now), \
             patch("betfair_results_downloader.scheduler.gap_detector.read_schedule_state", return_value=azure_row):
            from_dt, to_dt, reason = compute_backfill_window(BASE_CREDS, cfg)
        assert from_dt == datetime(2026, 6, 6, 15, 15, tzinfo=timezone.utc)
        assert to_dt == datetime(2026, 6, 6, 19, 30, tzinfo=timezone.utc)
        assert "LastConfirmedSettledAtUtc" in reason

    def test_csv_fallback_uses_timestamp_overlap(self, tmp_path: Path) -> None:
        csv_path = tmp_path / "cleared_orders_cleaned.csv"
        pd.DataFrame({"settledDate": ["2026-06-06T18:20:00Z"]}).to_csv(csv_path, index=False)
        creds = {**BASE_CREDS, "paths": {"results_csv_dir": str(tmp_path)}}
        cfg = _default_schedule_cfg(min_overlap_hours=2)
        fake_now = get_scheduler_now(cfg, datetime(2026, 6, 6, 19, 30, tzinfo=timezone.utc))
        with patch("betfair_results_downloader.scheduler.gap_detector.get_scheduler_now", return_value=fake_now), \
             patch("betfair_results_downloader.scheduler.gap_detector.read_schedule_state", return_value=None):
            from_dt, to_dt, _ = compute_backfill_window(creds, cfg)
        assert from_dt == datetime(2026, 6, 6, 16, 20, tzinfo=timezone.utc)
        assert to_dt == datetime(2026, 6, 6, 19, 30, tzinfo=timezone.utc)

    def test_uses_legacy_azure_local_coverage_when_confirmed_timestamp_missing(self) -> None:
        azure_row = ScheduleStateRow(
            user_id="TestUser",
            last_covered_date_utc=date(2026, 6, 5),
            last_covered_date_local=date(2026, 6, 6),
            last_covered_timezone="Australia/Sydney",
            last_confirmed_settled_at_utc=None,
            last_successful_download_started_utc=None,
            last_successful_download_finished_utc=None,
            last_run_started_utc=None,
            last_run_finished_utc=None,
            last_run_status="success",
            last_run_message=None,
            updated_utc=None,
        )
        cfg = _default_schedule_cfg(min_overlap_hours=2)
        fake_now = get_scheduler_now(cfg, datetime(2026, 6, 6, 19, 30, tzinfo=timezone.utc))
        with patch("betfair_results_downloader.scheduler.gap_detector.get_scheduler_now", return_value=fake_now), \
             patch("betfair_results_downloader.scheduler.gap_detector.read_schedule_state", return_value=azure_row), \
             patch("betfair_results_downloader.scheduler.gap_detector.resolve_results_dir", return_value=Path("/nonexistent")):
            from_dt, to_dt, reason = compute_backfill_window(BASE_CREDS, cfg)
        assert from_dt == datetime(2026, 6, 5, 22, 0, tzinfo=timezone.utc)
        assert to_dt == datetime(2026, 6, 6, 19, 30, tzinfo=timezone.utc)
        assert "LastCoveredDateLocal (legacy bootstrap)" in reason


class TestRunScheduled:
    def test_scheduled_run_no_longer_skips_after_local_marker_exists(self, tmp_path: Path) -> None:
        cfg = _default_schedule_cfg(log_dir=str(tmp_path))
        fake_now = get_scheduler_now(cfg, datetime(2026, 6, 6, 19, 30, tzinfo=timezone.utc))
        (tmp_path / "last_success_local_2026-06-07.marker").touch()

        with patch("betfair_results_downloader.scheduler.runner.get_scheduler_now", return_value=fake_now), \
             patch(
                 "betfair_results_downloader.scheduler.runner.compute_backfill_window",
                 return_value=(
                     datetime(2026, 6, 6, 17, 30, tzinfo=timezone.utc),
                     datetime(2026, 6, 6, 19, 30, tzinfo=timezone.utc),
                     "test",
                 ),
             ), \
             patch("betfair_results_downloader.scheduler.runner._run_pipeline") as run_pipeline, \
             patch("betfair_results_downloader.scheduler.runner.upsert_schedule_state", return_value=True):
            from betfair_results_downloader.scheduler.runner import RunResult
            run_pipeline.return_value = RunResult(
                ok=True,
                status="success",
                message="ok",
                last_confirmed_settled_at_utc=datetime(2026, 6, 6, 19, 20, tzinfo=timezone.utc),
                download_started_utc=datetime(2026, 6, 6, 19, 30, tzinfo=timezone.utc),
                download_finished_utc=datetime(2026, 6, 6, 19, 31, tzinfo=timezone.utc),
            )
            result = run_scheduled(BASE_CREDS, cfg)

        assert result.ok is True
        assert result.skipped is False

    def test_run_history_captures_confirmed_timestamp(self, tmp_path: Path) -> None:
        cfg = _default_schedule_cfg(log_dir=str(tmp_path))
        fake_now = get_scheduler_now(cfg, datetime(2026, 6, 6, 19, 30, tzinfo=timezone.utc))
        confirmed = datetime(2026, 6, 6, 19, 20, tzinfo=timezone.utc)

        with patch("betfair_results_downloader.scheduler.runner.get_scheduler_now", return_value=fake_now), \
             patch(
                 "betfair_results_downloader.scheduler.runner.compute_backfill_window",
                 return_value=(
                     datetime(2026, 6, 6, 17, 30, tzinfo=timezone.utc),
                     datetime(2026, 6, 6, 19, 30, tzinfo=timezone.utc),
                     "test",
                 ),
             ), \
             patch("betfair_results_downloader.scheduler.runner._run_pipeline") as run_pipeline, \
             patch("betfair_results_downloader.scheduler.runner.upsert_schedule_state", return_value=True):
            from betfair_results_downloader.scheduler.runner import RunResult
            run_pipeline.return_value = RunResult(
                ok=True,
                status="success",
                message="ok",
                last_confirmed_settled_at_utc=confirmed,
                download_started_utc=datetime(2026, 6, 6, 19, 30, tzinfo=timezone.utc),
                download_finished_utc=datetime(2026, 6, 6, 19, 31, tzinfo=timezone.utc),
            )
            run_scheduled(BASE_CREDS, cfg)

        record = json.loads((tmp_path / "run_history.jsonl").read_text().strip())
        assert record["last_confirmed_settled_at_utc"] == confirmed.isoformat()
        assert record["from_dt_utc"] == "2026-06-06T17:30:00+00:00"

    def test_empty_success_preserves_forward_checkpoint_in_state_update(self, tmp_path: Path) -> None:
        cfg = _default_schedule_cfg(log_dir=str(tmp_path))
        fake_now = get_scheduler_now(cfg, datetime(2026, 6, 6, 19, 30, tzinfo=timezone.utc))
        from_dt = datetime(2026, 6, 6, 17, 30, tzinfo=timezone.utc)
        to_dt = datetime(2026, 6, 6, 19, 30, tzinfo=timezone.utc)

        with patch("betfair_results_downloader.scheduler.runner.get_scheduler_now", return_value=fake_now), \
             patch(
                 "betfair_results_downloader.scheduler.runner.compute_backfill_window",
                 return_value=(from_dt, to_dt, "test"),
             ), \
             patch("betfair_results_downloader.scheduler.runner._run_pipeline") as run_pipeline, \
             patch("betfair_results_downloader.scheduler.runner.upsert_schedule_state", return_value=True) as upsert_state:
            from betfair_results_downloader.scheduler.runner import RunResult
            run_pipeline.return_value = RunResult(
                ok=True,
                status="success",
                message="Download returned no rows. ok",
                rows_downloaded=0,
                last_confirmed_settled_at_utc=to_dt,
                download_started_utc=from_dt,
                download_finished_utc=to_dt,
            )
            run_scheduled(BASE_CREDS, cfg)

        assert upsert_state.call_args.kwargs["last_confirmed_settled_at_utc"] == to_dt


class TestResolveResultsDir:
    def test_falls_back_to_get_results_database_dir_when_empty(self) -> None:
        sentinel = Path("/mock/onedrive/results")
        creds = {**BASE_CREDS, "paths": {"results_csv_dir": ""}}
        with patch("betfair_results_downloader.paths.get_results_database_dir", return_value=sentinel):
            result = _resolve_results_dir(creds)
        assert result == sentinel


class TestMaxSettledDateFromCsv:
    def test_returns_max_date(self, tmp_path: Path) -> None:
        csv_path = tmp_path / "cleared_orders_cleaned.csv"
        pd.DataFrame({"settledDate": [
            "2026-04-05T00:00:00Z",
            "2026-04-03T12:00:00Z",
            "2026-04-01T06:00:00Z",
        ]}).to_csv(csv_path, index=False)
        assert _max_settled_date_from_csv(tmp_path) == date(2026, 4, 5)

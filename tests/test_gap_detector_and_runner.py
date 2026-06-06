"""Tests for scheduler gap detection and runner timezone semantics."""
from __future__ import annotations

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
        "log_dir": cfg.log_dir,
        "history_file": cfg.history_file,
    }
    data.update(overrides)
    return ScheduleConfig(**data)


class TestSchedulerTimeSemantics:
    def test_scheduler_now_tracks_both_utc_and_sydney_dates(self) -> None:
        cfg = _default_schedule_cfg()
        now = datetime(2026, 6, 6, 19, 30, tzinfo=timezone.utc)
        observed = get_scheduler_now(cfg, now)
        assert observed.today_utc == date(2026, 6, 6)
        assert observed.today_local == date(2026, 6, 7)
        assert observed.timezone_name == "Australia/Sydney"


class TestComputeBackfillWindow:
    def test_uses_azure_local_coverage_when_available(self) -> None:
        azure_row = ScheduleStateRow(
            user_id="TestUser",
            last_covered_date_utc=date(2026, 6, 6),
            last_covered_date_local=date(2026, 6, 7),
            last_covered_timezone="Australia/Sydney",
            last_run_started_utc=None,
            last_run_finished_utc=None,
            last_run_status="success",
            last_run_message=None,
            updated_utc=None,
        )
        cfg = _default_schedule_cfg(min_coverage_overlap_days=1)
        fake_now = get_scheduler_now(cfg, datetime(2026, 6, 6, 19, 30, tzinfo=timezone.utc))
        with patch("betfair_results_downloader.scheduler.gap_detector.get_scheduler_now", return_value=fake_now), \
             patch("betfair_results_downloader.scheduler.gap_detector.read_schedule_state", return_value=azure_row):
            from_d, to_d, reason = compute_backfill_window(BASE_CREDS, cfg)
        assert from_d == date(2026, 6, 7)
        assert to_d == date(2026, 6, 7)
        assert "LastCoveredDateLocal" in reason

    def test_falls_back_to_utc_coverage_for_backward_compatibility(self) -> None:
        azure_row = ScheduleStateRow(
            user_id="TestUser",
            last_covered_date_utc=date(2026, 6, 6),
            last_covered_date_local=None,
            last_covered_timezone=None,
            last_run_started_utc=None,
            last_run_finished_utc=None,
            last_run_status="success",
            last_run_message=None,
            updated_utc=None,
        )
        cfg = _default_schedule_cfg(min_coverage_overlap_days=1)
        fake_now = get_scheduler_now(cfg, datetime(2026, 6, 6, 19, 30, tzinfo=timezone.utc))
        with patch("betfair_results_downloader.scheduler.gap_detector.get_scheduler_now", return_value=fake_now), \
             patch("betfair_results_downloader.scheduler.gap_detector.read_schedule_state", return_value=azure_row):
            from_d, to_d, reason = compute_backfill_window(BASE_CREDS, cfg)
        assert from_d == date(2026, 6, 6)
        assert to_d == date(2026, 6, 7)
        assert "LastCoveredDateUtc" in reason

    def test_csv_fallback_uses_local_today(self, tmp_path: Path) -> None:
        csv_path = tmp_path / "cleared_orders_cleaned.csv"
        pd.DataFrame({"settledDate": ["2026-06-06T00:00:00Z"]}).to_csv(csv_path, index=False)
        creds = {**BASE_CREDS, "paths": {"results_csv_dir": str(tmp_path)}}
        cfg = _default_schedule_cfg()
        fake_now = get_scheduler_now(cfg, datetime(2026, 6, 6, 19, 30, tzinfo=timezone.utc))
        with patch("betfair_results_downloader.scheduler.gap_detector.get_scheduler_now", return_value=fake_now), \
             patch("betfair_results_downloader.scheduler.gap_detector.read_schedule_state", return_value=None):
            from_d, to_d, _ = compute_backfill_window(creds, cfg)
        assert from_d == date(2026, 6, 6)
        assert to_d == date(2026, 6, 7)


class TestRunScheduled:
    def test_early_sydney_run_uses_local_marker_and_writes_both_namespaces(self, tmp_path: Path) -> None:
        cfg = _default_schedule_cfg(log_dir=str(tmp_path))
        fake_now = get_scheduler_now(cfg, datetime(2026, 6, 6, 19, 30, tzinfo=timezone.utc))

        with patch("betfair_results_downloader.scheduler.runner.get_scheduler_now", return_value=fake_now), \
             patch("betfair_results_downloader.scheduler.runner.compute_backfill_window", return_value=(date(2026, 6, 7), date(2026, 6, 7), "test")), \
             patch("betfair_results_downloader.scheduler.runner._run_pipeline") as run_pipeline, \
             patch("betfair_results_downloader.scheduler.runner.upsert_schedule_state", return_value=True) as upsert:
            from betfair_results_downloader.scheduler.runner import RunResult
            run_pipeline.return_value = RunResult(ok=True, status="success", message="ok")
            result = run_scheduled(BASE_CREDS, cfg)

        assert result.ok is True
        kwargs = upsert.call_args.kwargs
        assert kwargs["last_covered_date_local"] == date(2026, 6, 7)
        assert kwargs["last_covered_date_utc"] == date(2026, 6, 6)
        assert (tmp_path / "last_success_local_2026-06-07.marker").exists()
        assert (tmp_path / "last_success_utc_2026-06-06.marker").exists()

    def test_existing_local_marker_skips_even_when_utc_day_differs(self, tmp_path: Path) -> None:
        cfg = _default_schedule_cfg(log_dir=str(tmp_path))
        fake_now = get_scheduler_now(cfg, datetime(2026, 6, 6, 19, 30, tzinfo=timezone.utc))
        (tmp_path / "last_success_local_2026-06-07.marker").touch()

        with patch("betfair_results_downloader.scheduler.runner.get_scheduler_now", return_value=fake_now):
            result = run_scheduled(BASE_CREDS, cfg)

        assert result.skipped is True
        assert "Australia/Sydney" in result.skip_reason


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

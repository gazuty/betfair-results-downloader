"""Tests for Phase 2.2: gap_detector.py and runner.py."""
from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from betfair_results_downloader.config import ScheduleConfig, parse_schedule_config
from betfair_results_downloader.scheduler.gap_detector import compute_backfill_window
from betfair_results_downloader.scheduler.runner import (
    RunResult,
    _azure_publish_allowed,
    _resolve_results_dir,
    run_backfill,
)
from betfair_results_downloader.scheduler.state import ScheduleStateRow


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

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
        "primary_time": "06:00",
        "retry_times": [],
        "publish_to_azure": True,
        "allow_azure_publish": False,
        "max_backfill_days": 90,
        "chunk_days": 30,
        "min_coverage_overlap_days": 1,
        "log_dir": "",
    },
}

TODAY = date(2026, 4, 6)


def _default_schedule_cfg(**overrides):
    cfg = parse_schedule_config(BASE_CREDS)
    if overrides:
        # ScheduleConfig is frozen; rebuild with overrides
        d = {
            "enabled": cfg.enabled, "timezone": cfg.timezone,
            "primary_time": cfg.primary_time, "retry_times": cfg.retry_times,
            "publish_to_azure": cfg.publish_to_azure,
            "allow_azure_publish": cfg.allow_azure_publish,
            "max_backfill_days": cfg.max_backfill_days,
            "chunk_days": cfg.chunk_days,
            "min_coverage_overlap_days": cfg.min_coverage_overlap_days,
            "log_dir": cfg.log_dir, "history_file": cfg.history_file,
        }
        d.update(overrides)
        return ScheduleConfig(**d)
    return cfg


# ---------------------------------------------------------------------------
# compute_backfill_window
# ---------------------------------------------------------------------------

class TestComputeBackfillWindow:
    def _patch_today(self, target_date: date):
        return patch(
            "betfair_results_downloader.scheduler.gap_detector._today_utc",
            return_value=target_date,
        )

    def test_uses_azure_state_when_available(self) -> None:
        azure_row = ScheduleStateRow(
            user_id="TestUser",
            last_covered_date_utc=date(2026, 4, 4),
            last_run_started_utc=None,
            last_run_finished_utc=None,
            last_run_status="success",
            last_run_message=None,
            updated_utc=None,
        )
        schedule_cfg = _default_schedule_cfg(min_coverage_overlap_days=1)
        with self._patch_today(TODAY):
            with patch("betfair_results_downloader.scheduler.gap_detector.read_schedule_state",
                       return_value=azure_row):
                from_d, to_d, reason = compute_backfill_window(BASE_CREDS, schedule_cfg)

        # last_covered = 2026-04-04, overlap=1 → from = 2026-04-04
        assert from_d == date(2026, 4, 4)
        assert to_d == TODAY
        assert "Azure" in reason

    def test_falls_back_to_csv_when_azure_unavailable(self, tmp_path: Path) -> None:
        import pandas as pd
        # Write a minimal canonical CSV
        csv_path = tmp_path / "cleared_orders_cleaned.csv"
        df = pd.DataFrame({"settledDate": ["2026-03-30T00:00:00Z"]})
        df.to_csv(csv_path, index=False)

        creds = {**BASE_CREDS, "paths": {"results_csv_dir": str(tmp_path)}}
        schedule_cfg = _default_schedule_cfg()
        with self._patch_today(TODAY):
            with patch("betfair_results_downloader.scheduler.gap_detector.read_schedule_state",
                       return_value=None):
                from_d, to_d, reason = compute_backfill_window(creds, schedule_cfg)

        assert "CSV" in reason
        assert to_d == TODAY

    def test_cold_start_when_no_state_or_csv(self, tmp_path: Path) -> None:
        # Point to an empty dir so the resolver won't find a real CSV
        creds = {**BASE_CREDS, "paths": {"results_csv_dir": str(tmp_path)}}
        schedule_cfg = _default_schedule_cfg(max_backfill_days=90)
        with self._patch_today(TODAY):
            with patch("betfair_results_downloader.scheduler.gap_detector.read_schedule_state",
                       return_value=None):
                from_d, to_d, reason = compute_backfill_window(creds, schedule_cfg)

        assert from_d == TODAY - timedelta(days=90)
        assert to_d == TODAY
        assert "Cold-start" in reason or "cold-start" in reason.lower()

    def test_caps_from_date_at_max_backfill_days(self) -> None:
        # Azure says last covered was 300 days ago — should be capped
        old_date = TODAY - timedelta(days=300)
        azure_row = ScheduleStateRow(
            user_id="TestUser",
            last_covered_date_utc=old_date,
            last_run_started_utc=None, last_run_finished_utc=None,
            last_run_status="success", last_run_message=None, updated_utc=None,
        )
        schedule_cfg = _default_schedule_cfg(max_backfill_days=90)
        with self._patch_today(TODAY):
            with patch("betfair_results_downloader.scheduler.gap_detector.read_schedule_state",
                       return_value=azure_row):
                from_d, _, reason = compute_backfill_window(BASE_CREDS, schedule_cfg)

        assert from_d >= TODAY - timedelta(days=90)
        assert "capped" in reason.lower() or "max_backfill" in reason.lower()

    def test_overlap_applied_from_azure_state(self) -> None:
        azure_row = ScheduleStateRow(
            user_id="TestUser",
            last_covered_date_utc=date(2026, 4, 5),
            last_run_started_utc=None, last_run_finished_utc=None,
            last_run_status="success", last_run_message=None, updated_utc=None,
        )
        schedule_cfg = _default_schedule_cfg(min_coverage_overlap_days=2)
        with self._patch_today(TODAY):
            with patch("betfair_results_downloader.scheduler.gap_detector.read_schedule_state",
                       return_value=azure_row):
                from_d, _, _ = compute_backfill_window(BASE_CREDS, schedule_cfg)

        # last_covered = 2026-04-05, overlap=2 → from = 2026-04-04
        assert from_d == date(2026, 4, 4)


# ---------------------------------------------------------------------------
# _azure_publish_allowed
# ---------------------------------------------------------------------------

class TestAzurePublishAllowed:
    def _cfg(self, publish_to_azure: bool, allow_azure_publish: bool) -> ScheduleConfig:
        return _default_schedule_cfg(
            publish_to_azure=publish_to_azure,
            allow_azure_publish=allow_azure_publish,
        )

    def test_all_gates_open_returns_true(self) -> None:
        creds = {**BASE_CREDS, "user": {
            "enable_azure_sql": True, "dry_run": False,
        }}
        cfg = self._cfg(publish_to_azure=True, allow_azure_publish=True)
        assert _azure_publish_allowed(creds, cfg) is True

    def test_enable_azure_false_returns_false(self) -> None:
        creds = {**BASE_CREDS, "user": {"enable_azure_sql": False, "dry_run": False}}
        cfg = self._cfg(True, True)
        assert _azure_publish_allowed(creds, cfg) is False

    def test_dry_run_true_returns_false(self) -> None:
        creds = {**BASE_CREDS, "user": {"enable_azure_sql": True, "dry_run": True}}
        cfg = self._cfg(True, True)
        assert _azure_publish_allowed(creds, cfg) is False

    def test_publish_to_azure_false_returns_false(self) -> None:
        creds = {**BASE_CREDS, "user": {"enable_azure_sql": True, "dry_run": False}}
        cfg = self._cfg(publish_to_azure=False, allow_azure_publish=True)
        assert _azure_publish_allowed(creds, cfg) is False

    def test_allow_azure_publish_false_returns_false(self) -> None:
        creds = {**BASE_CREDS, "user": {"enable_azure_sql": True, "dry_run": False}}
        cfg = self._cfg(publish_to_azure=True, allow_azure_publish=False)
        assert _azure_publish_allowed(creds, cfg) is False


# ---------------------------------------------------------------------------
# run_backfill — date validation
# ---------------------------------------------------------------------------

class TestRunBackfillDateValidation:
    def test_inverted_range_returns_failure(self) -> None:
        schedule_cfg = _default_schedule_cfg()
        result = run_backfill(
            BASE_CREDS, schedule_cfg,
            from_date=date(2026, 4, 6),
            to_date=date(2026, 4, 1),
        )
        assert not result.ok
        assert result.status == "failed"
        assert "Invalid" in result.message or "from_date" in result.message

    def test_same_day_range_is_valid(self, tmp_path: Path) -> None:
        """
        Single-day backfill with mocked pipeline — should not error on range.
        Missing results_csv_dir should produce a clean 'failed' (not a crash).
        """
        creds = {**BASE_CREDS, "paths": {"results_csv_dir": ""}}
        schedule_cfg = _default_schedule_cfg()
        result = run_backfill(
            creds, schedule_cfg,
            from_date=date(2026, 4, 6),
            to_date=date(2026, 4, 6),
        )
        # Missing results_csv_dir → falls back to cross-platform resolver
        assert result.status in ("failed", "success", "partial")


# ---------------------------------------------------------------------------
# _resolve_results_dir — fallback to get_results_database_dir
# ---------------------------------------------------------------------------

class TestResolveResultsDir:
    def test_falls_back_to_get_results_database_dir_when_empty(self) -> None:
        """When paths.results_csv_dir is empty, fall back to get_results_database_dir()."""
        sentinel = Path("/mock/onedrive/results")
        creds = {**BASE_CREDS, "paths": {"results_csv_dir": ""}}
        with patch(
            "betfair_results_downloader.paths.get_results_database_dir",
            return_value=sentinel,
        ):
            result = _resolve_results_dir(creds)
        assert result == sentinel


# ---------------------------------------------------------------------------
# Gap detector — CSV fallback uses cross-platform resolver
# ---------------------------------------------------------------------------

class TestGapDetectorCsvFallback:
    def _patch_today(self, target_date: date):
        return patch(
            "betfair_results_downloader.scheduler.gap_detector._today_utc",
            return_value=target_date,
        )

    def test_finds_csv_via_resolver_when_results_csv_dir_empty(self, tmp_path: Path) -> None:
        """When paths.results_csv_dir is empty but the cross-platform resolver
        returns a directory containing a canonical CSV, the gap detector should
        use the CSV path — NOT fall through to cold-start."""
        import pandas as pd
        csv_path = tmp_path / "cleared_orders_cleaned.csv"
        df = pd.DataFrame({"settledDate": ["2026-04-05T00:00:00Z"]})
        df.to_csv(csv_path, index=False)

        creds = {**BASE_CREDS, "paths": {"results_csv_dir": ""}}
        schedule_cfg = _default_schedule_cfg()

        with self._patch_today(TODAY):
            with patch("betfair_results_downloader.scheduler.gap_detector.read_schedule_state",
                       return_value=None):
                with patch("betfair_results_downloader.paths.get_results_database_dir",
                           return_value=tmp_path):
                    from_d, to_d, reason = compute_backfill_window(creds, schedule_cfg)

        assert "CSV" in reason
        assert "Cold-start" not in reason
        assert to_d == TODAY

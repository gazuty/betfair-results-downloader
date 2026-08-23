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

    def test_uses_legacy_azure_utc_coverage_when_confirmed_timestamp_missing(self) -> None:
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
        assert from_dt == datetime(2026, 6, 4, 22, 0, tzinfo=timezone.utc)
        assert to_dt == datetime(2026, 6, 6, 19, 30, tzinfo=timezone.utc)
        assert "LastCoveredDateUtc (legacy bootstrap)" in reason

    def test_local_legacy_bootstrap_uses_recorded_timezone_before_capping(self) -> None:
        azure_row = ScheduleStateRow(
            user_id="TestUser",
            last_covered_date_utc=None,
            last_covered_date_local=date(2026, 6, 7),
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
        fake_now = get_scheduler_now(cfg, datetime(2026, 6, 7, 5, 0, tzinfo=timezone.utc))
        with patch("betfair_results_downloader.scheduler.gap_detector.get_scheduler_now", return_value=fake_now), \
             patch("betfair_results_downloader.scheduler.gap_detector.read_schedule_state", return_value=azure_row), \
             patch("betfair_results_downloader.scheduler.gap_detector.resolve_results_dir", return_value=Path("/nonexistent")):
            from_dt, to_dt, reason = compute_backfill_window(BASE_CREDS, cfg)
        assert to_dt == datetime(2026, 6, 7, 5, 0, tzinfo=timezone.utc)
        assert from_dt == datetime(2026, 6, 6, 12, 0, tzinfo=timezone.utc)
        assert from_dt <= to_dt
        assert "tz=Australia/Sydney" in reason


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

    def test_empty_success_forwards_none_checkpoint_so_db_keeps_previous(self, tmp_path: Path) -> None:
        """An empty download confirms nothing: the runner passes None and the
        upsert SQL keeps the stored checkpoint instead of advancing it."""
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
                last_confirmed_settled_at_utc=None,
                download_started_utc=from_dt,
                download_finished_utc=to_dt,
            )
            run_scheduled(BASE_CREDS, cfg)

        assert upsert_state.call_args.kwargs["last_confirmed_settled_at_utc"] is None

    def test_failed_run_appends_history_without_advancing_state(self, tmp_path: Path) -> None:
        cfg = _default_schedule_cfg(log_dir=str(tmp_path))
        fake_now = get_scheduler_now(cfg, datetime(2026, 6, 6, 19, 30, tzinfo=timezone.utc))

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
             patch("betfair_results_downloader.scheduler.runner.upsert_schedule_state", return_value=True) as upsert_state:
            from betfair_results_downloader.scheduler.runner import RunResult
            run_pipeline.return_value = RunResult(ok=False, status="failed", message="boom")
            result = run_scheduled(BASE_CREDS, cfg)

        assert result.ok is False
        upsert_state.assert_not_called()
        record = json.loads((tmp_path / "run_history.jsonl").read_text().strip())
        assert record["status"] == "failed"


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


class TestRunPipelineOutcomes:
    """_run_pipeline must never raise, and must report honest outcomes."""

    def _creds(self, tmp_path: Path, *, azure_gates_open: bool = False) -> dict:
        creds = json.loads(json.dumps(BASE_CREDS))
        creds["paths"]["results_csv_dir"] = str(tmp_path)
        if azure_gates_open:
            creds["user"]["enable_azure_sql"] = True
            creds["user"]["dry_run"] = False
            creds["schedule"]["allow_azure_publish"] = True
        return creds

    def test_download_exception_returns_failed_result_instead_of_raising(self, tmp_path: Path) -> None:
        from unittest.mock import MagicMock
        from betfair_results_downloader.scheduler.runner import _run_pipeline

        creds = self._creds(tmp_path)
        cfg = _default_schedule_cfg()
        from_dt = datetime(2026, 6, 6, 17, 30, tzinfo=timezone.utc)
        to_dt = datetime(2026, 6, 6, 19, 30, tzinfo=timezone.utc)

        with patch("betfair_results_downloader.scheduler.runner.build_api_client", return_value=MagicMock()), \
             patch(
                 "betfair_results_downloader.downloader_core.fetch_cleared_orders_df_range",
                 side_effect=RuntimeError("network exploded"),
             ):
            result = _run_pipeline(creds, cfg, from_dt, to_dt)

        assert result.ok is False
        assert result.status == "failed"
        assert "network exploded" in result.message

    def test_empty_download_leaves_checkpoint_unset(self, tmp_path: Path) -> None:
        from unittest.mock import MagicMock
        from betfair_results_downloader.downloader_core import DownloadResult
        from betfair_results_downloader.scheduler.runner import _run_pipeline

        creds = self._creds(tmp_path)
        cfg = _default_schedule_cfg()
        from_dt = datetime(2026, 6, 6, 17, 30, tzinfo=timezone.utc)
        to_dt = datetime(2026, 6, 6, 19, 30, tzinfo=timezone.utc)
        empty = DownloadResult(attempted=True, rows_downloaded=0, message="none", df_co=pd.DataFrame())

        with patch("betfair_results_downloader.scheduler.runner.build_api_client", return_value=MagicMock()), \
             patch(
                 "betfair_results_downloader.downloader_core.fetch_cleared_orders_df_range",
                 return_value=empty,
             ):
            result = _run_pipeline(creds, cfg, from_dt, to_dt)

        assert result.ok is True
        assert result.status == "success"
        assert result.last_confirmed_settled_at_utc is None

    def test_failed_publish_yields_partial_status_not_published(self, tmp_path: Path) -> None:
        from unittest.mock import MagicMock
        from betfair_results_downloader.azure_publish import AzurePublishResult
        from betfair_results_downloader.downloader_core import (
            AzurePrepResult,
            CsvWriteResult,
            DownloadResult,
            EnrichResult,
        )
        from betfair_results_downloader.scheduler.runner import _run_pipeline
        from decimal import Decimal

        creds = self._creds(tmp_path, azure_gates_open=True)
        cfg = _default_schedule_cfg(allow_azure_publish=True)
        from_dt = datetime(2026, 6, 6, 17, 30, tzinfo=timezone.utc)
        to_dt = datetime(2026, 6, 6, 19, 30, tzinfo=timezone.utc)

        df = pd.DataFrame({
            "betId": ["1"],
            "eventTypeId": [7],
            "marketId": ["1.234"],
            "profit": [5.0],
            "placedDate": ["2026-06-06T17:45:00Z"],
            "settledDate": ["2026-06-06T18:00:00Z"],
        })
        dl = DownloadResult(attempted=True, rows_downloaded=1, message="ok", df_co=df)
        enr = (df, EnrichResult(attempted=True, markets_requested=0, markets_returned=0, message="ok"))
        csvr = CsvWriteResult(
            canonical_path=tmp_path / "cleared_orders_cleaned.csv",
            snapshot_path=tmp_path / "snap.csv.gz",
            rows_in_canonical=1,
            message="ok",
        )
        prep = AzurePrepResult(
            attempted=True, rows_after_filter=1, markets_aggregated=1, message="ok",
            rows_to_write=[(Decimal("1.234"), Decimal("5.00"), "")],
        )
        failed_publish = AzurePublishResult(attempted=True, ok=False, message="Azure publish failed: boom")

        with patch("betfair_results_downloader.scheduler.runner.build_api_client", return_value=MagicMock()), \
             patch("betfair_results_downloader.downloader_core.fetch_cleared_orders_df_range", return_value=dl), \
             patch("betfair_results_downloader.downloader_core.enrich_with_market_catalogue", return_value=enr), \
             patch("betfair_results_downloader.downloader_core.write_csv_outputs", return_value=csvr), \
             patch("betfair_results_downloader.downloader_core.prepare_azure_dataset", return_value=prep), \
             patch("betfair_results_downloader.azure_publish.publish_to_azure_sql", return_value=failed_publish):
            result = _run_pipeline(creds, cfg, from_dt, to_dt)

        assert result.ok is True
        assert result.status == "partial"
        assert result.azure_published is False
        assert "Azure publish failed" in result.message

    def test_enrichment_failure_is_non_fatal(self, tmp_path: Path) -> None:
        from unittest.mock import MagicMock
        from betfair_results_downloader.downloader_core import CsvWriteResult, DownloadResult
        from betfair_results_downloader.scheduler.runner import _run_pipeline

        creds = self._creds(tmp_path)
        cfg = _default_schedule_cfg()
        from_dt = datetime(2026, 6, 6, 17, 30, tzinfo=timezone.utc)
        to_dt = datetime(2026, 6, 6, 19, 30, tzinfo=timezone.utc)

        df = pd.DataFrame({
            "betId": ["1"],
            "settledDate": ["2026-06-06T18:00:00Z"],
        })
        dl = DownloadResult(attempted=True, rows_downloaded=1, message="ok", df_co=df)
        csvr = CsvWriteResult(
            canonical_path=tmp_path / "cleared_orders_cleaned.csv",
            snapshot_path=tmp_path / "snap.csv.gz",
            rows_in_canonical=1,
            message="ok",
        )

        with patch("betfair_results_downloader.scheduler.runner.build_api_client", return_value=MagicMock()), \
             patch("betfair_results_downloader.downloader_core.fetch_cleared_orders_df_range", return_value=dl), \
             patch(
                 "betfair_results_downloader.downloader_core.enrich_with_market_catalogue",
                 side_effect=RuntimeError("catalogue down"),
             ), \
             patch("betfair_results_downloader.downloader_core.write_csv_outputs", return_value=csvr) as write_csv:
            result = _run_pipeline(creds, cfg, from_dt, to_dt)

        assert result.ok is True
        assert result.status == "success"
        write_csv.assert_called_once()

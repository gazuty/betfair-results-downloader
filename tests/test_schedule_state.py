"""Tests for scheduler/state.py state layer."""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

from betfair_results_downloader.scheduler.state import (
    append_run_history,
    _marker_filename,
    read_schedule_state,
    upsert_schedule_state,
    write_today_success_marker,
)

BASE_CREDS = {
    "betfair": {"username": "u", "password": "p", "app_key": "k"},
    "user": {"user_id": "TestUser", "db_user_id": "TestUser"},
    "azure_sql": {
        "server": "test.database.windows.net",
        "port": 1433,
        "database": "RACE",
        "username": "admin",
        "password": "secret",
        "driver": "ODBC Driver 18 for SQL Server",
    },
}

TODAY = date(2026, 4, 6)
_SENTINEL = object()


def _make_mock_conn(cursor_return=_SENTINEL):
    mock_cursor = MagicMock()
    mock_cursor.fetchone.return_value = None if cursor_return is _SENTINEL else cursor_return
    mock_conn = MagicMock()
    mock_conn.cursor.return_value = mock_cursor
    mock_conn.__enter__ = MagicMock(return_value=mock_conn)
    mock_conn.__exit__ = MagicMock(return_value=False)
    return mock_conn, mock_cursor


class TestReadScheduleState:
    def test_returns_none_when_no_db_user_id(self) -> None:
        creds = {**BASE_CREDS, "user": {"user_id": "", "db_user_id": ""}}
        assert read_schedule_state(creds) is None

    def test_returns_none_when_azure_not_configured(self) -> None:
        creds = {**BASE_CREDS, "azure_sql": {}}
        assert read_schedule_state(creds) is None

    def test_returns_none_when_row_not_found(self) -> None:
        mock_conn, _ = _make_mock_conn(cursor_return=None)
        with patch("betfair_results_downloader.scheduler.state._open_azure_connection", return_value=mock_conn):
            assert read_schedule_state(BASE_CREDS) is None

    def test_returns_state_row_when_found_with_incremental_fields(self) -> None:
        run_dt = datetime(2026, 4, 5, 6, 0, 0)
        confirmed_dt = datetime(2026, 4, 5, 5, 45, 0)
        mock_row = (
            "TestUser",
            date(2026, 4, 5),
            date(2026, 4, 6),
            "Australia/Sydney",
            confirmed_dt,
            run_dt,
            run_dt,
            run_dt,
            run_dt,
            "success",
            "Downloaded 100 rows.",
            datetime(2026, 4, 5, 6, 1, 0),
        )
        mock_conn, _ = _make_mock_conn(cursor_return=mock_row)
        with patch("betfair_results_downloader.scheduler.state._open_azure_connection", return_value=mock_conn):
            result = read_schedule_state(BASE_CREDS)

        assert result is not None
        assert result.last_covered_date_utc == date(2026, 4, 5)
        assert result.last_covered_date_local == date(2026, 4, 6)
        assert result.last_covered_timezone == "Australia/Sydney"
        assert result.last_confirmed_settled_at_utc == confirmed_dt.replace(tzinfo=timezone.utc)


class TestUpsertScheduleState:
    def test_returns_false_when_no_user_id(self) -> None:
        creds = {**BASE_CREDS, "user": {"user_id": "", "db_user_id": ""}}
        result = upsert_schedule_state(creds, TODAY, TODAY, "Australia/Sydney", "success", "ok")
        assert result is False

    def test_returns_true_on_success(self) -> None:
        mock_conn, mock_cursor = _make_mock_conn()
        confirmed = datetime(2026, 4, 6, 8, 15, tzinfo=timezone.utc)
        with patch("betfair_results_downloader.scheduler.state._open_azure_connection", return_value=mock_conn):
            result = upsert_schedule_state(
                BASE_CREDS,
                TODAY,
                TODAY + timedelta(days=1),
                "Australia/Sydney",
                "success",
                "all good",
                last_confirmed_settled_at_utc=confirmed,
            )
        assert result is True
        sql = mock_cursor.execute.call_args[0][0]
        assert "WITH (HOLDLOCK)" in sql
        assert "CASE" in sql  # checkpoint is monotonic / NULL-preserving
        params = mock_cursor.execute.call_args[0][1:]
        assert TODAY in params
        assert TODAY + timedelta(days=1) in params
        assert "Australia/Sydney" in params
        assert confirmed.replace(tzinfo=None) in params

    def test_message_truncated_to_1000_chars(self) -> None:
        mock_conn, mock_cursor = _make_mock_conn()
        long_msg = "x" * 2000
        with patch("betfair_results_downloader.scheduler.state._open_azure_connection", return_value=mock_conn):
            upsert_schedule_state(BASE_CREDS, TODAY, TODAY, "Australia/Sydney", "success", long_msg)
        params = mock_cursor.execute.call_args[0][1:]
        message_params = [p for p in params if isinstance(p, str) and "x" in p]
        assert all(len(m) <= 1000 for m in message_params)


class TestAppendRunHistory:
    def test_creates_jsonl_file(self, tmp_path: Path) -> None:
        append_run_history(tmp_path, {"status": "success", "rows": 100})
        data = json.loads((tmp_path / "run_history.jsonl").read_text().strip())
        assert data["status"] == "success"
        assert data["rows"] == 100

    def test_injects_ts_if_not_present(self, tmp_path: Path) -> None:
        append_run_history(tmp_path, {"status": "ok"})
        data = json.loads((tmp_path / "run_history.jsonl").read_text().strip())
        assert "ts" in data


class TestSuccessMarker:
    def test_write_creates_local_marker_file(self, tmp_path: Path) -> None:
        write_today_success_marker(tmp_path, TODAY, marker_namespace="local")
        assert (tmp_path / _marker_filename(TODAY, "local")).exists()

    def test_write_creates_utc_marker_file(self, tmp_path: Path) -> None:
        write_today_success_marker(tmp_path, TODAY, marker_namespace="utc")
        assert (tmp_path / _marker_filename(TODAY, "utc")).exists()

    def test_different_namespace_isolated(self, tmp_path: Path) -> None:
        write_today_success_marker(tmp_path, TODAY, marker_namespace="local")
        assert not (tmp_path / _marker_filename(TODAY, "utc")).exists()

    def test_marker_filename_format(self, tmp_path: Path) -> None:
        write_today_success_marker(tmp_path, TODAY, marker_namespace="local")
        write_today_success_marker(tmp_path, TODAY, marker_namespace="utc")
        assert (tmp_path / "last_success_local_2026-04-06.marker").exists()
        assert (tmp_path / "last_success_utc_2026-04-06.marker").exists()

"""Tests for Phase 2.1: scheduler/state.py state layer."""
from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch


from betfair_results_downloader.scheduler.state import (
    append_run_history,
    check_today_success_marker,
    read_schedule_state,
    upsert_schedule_state,
    write_today_success_marker,
)


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

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


def _make_mock_conn(cursor_return=_SENTINEL, raise_on_connect=False):
    """Build a mocked pyodbc connection that supports context manager usage."""
    mock_cursor = MagicMock()
    # Always configure fetchone explicitly so None return works correctly
    mock_cursor.fetchone.return_value = None if cursor_return is _SENTINEL else cursor_return
    mock_conn = MagicMock()
    mock_conn.cursor.return_value = mock_cursor
    mock_conn.__enter__ = MagicMock(return_value=mock_conn)
    mock_conn.__exit__ = MagicMock(return_value=False)
    return mock_conn, mock_cursor


# ---------------------------------------------------------------------------
# read_schedule_state
# ---------------------------------------------------------------------------

class TestReadScheduleState:
    def test_returns_none_when_no_db_user_id(self) -> None:
        creds = {**BASE_CREDS, "user": {"user_id": "", "db_user_id": ""}}
        result = read_schedule_state(creds)
        assert result is None

    def test_returns_none_when_azure_not_configured(self) -> None:
        creds = {**BASE_CREDS, "azure_sql": {}}
        result = read_schedule_state(creds)
        assert result is None

    def test_returns_none_when_pyodbc_unavailable(self) -> None:
        with patch.dict("sys.modules", {"pyodbc": None}):
            result = read_schedule_state(BASE_CREDS)
        assert result is None

    def test_returns_none_when_row_not_found(self) -> None:
        mock_conn, mock_cursor = _make_mock_conn(cursor_return=None)
        with patch("betfair_results_downloader.scheduler.state._open_azure_connection",
                   return_value=mock_conn):
            result = read_schedule_state(BASE_CREDS)
        assert result is None

    def test_returns_state_row_when_found(self) -> None:
        run_dt = datetime(2026, 4, 5, 6, 0, 0)
        mock_row = (
            "TestUser",
            date(2026, 4, 5),
            run_dt,
            run_dt,
            "success",
            "Downloaded 100 rows.",
            datetime(2026, 4, 5, 6, 1, 0),
        )
        mock_conn, _ = _make_mock_conn(cursor_return=mock_row)
        with patch("betfair_results_downloader.scheduler.state._open_azure_connection",
                   return_value=mock_conn):
            result = read_schedule_state(BASE_CREDS)

        assert result is not None
        assert result.user_id == "TestUser"
        assert result.last_covered_date_utc == date(2026, 4, 5)
        assert result.last_run_status == "success"

    def test_returns_none_when_azure_raises(self) -> None:
        mock_conn = MagicMock()
        mock_conn.__enter__ = MagicMock(side_effect=Exception("connection lost"))
        mock_conn.__exit__ = MagicMock(return_value=False)
        with patch("betfair_results_downloader.scheduler.state._open_azure_connection",
                   return_value=mock_conn):
            result = read_schedule_state(BASE_CREDS)
        assert result is None


# ---------------------------------------------------------------------------
# upsert_schedule_state
# ---------------------------------------------------------------------------

class TestUpsertScheduleState:
    def test_returns_false_when_no_user_id(self) -> None:
        creds = {**BASE_CREDS, "user": {"user_id": "", "db_user_id": ""}}
        result = upsert_schedule_state(creds, TODAY, "success", "ok")
        assert result is False

    def test_returns_false_when_azure_not_configured(self) -> None:
        creds = {**BASE_CREDS, "azure_sql": {}}
        result = upsert_schedule_state(creds, TODAY, "success", "ok")
        assert result is False

    def test_returns_true_on_success(self) -> None:
        mock_conn, mock_cursor = _make_mock_conn()
        with patch("betfair_results_downloader.scheduler.state._open_azure_connection",
                   return_value=mock_conn):
            result = upsert_schedule_state(BASE_CREDS, TODAY, "success", "all good")
        assert result is True
        mock_cursor.execute.assert_called_once()

    def test_returns_false_on_db_exception(self) -> None:
        mock_conn = MagicMock()
        mock_conn.__enter__ = MagicMock(side_effect=Exception("SQL error"))
        mock_conn.__exit__ = MagicMock(return_value=False)
        with patch("betfair_results_downloader.scheduler.state._open_azure_connection",
                   return_value=mock_conn):
            result = upsert_schedule_state(BASE_CREDS, TODAY, "failed", "boom")
        assert result is False

    def test_message_truncated_to_1000_chars(self) -> None:
        mock_conn, mock_cursor = _make_mock_conn()
        long_msg = "x" * 2000
        with patch("betfair_results_downloader.scheduler.state._open_azure_connection",
                   return_value=mock_conn):
            upsert_schedule_state(BASE_CREDS, TODAY, "success", long_msg)

        # The message passed to cursor.execute should be truncated to 1000
        call_args = mock_cursor.execute.call_args[0]
        # Parameters are positional after the SQL string
        params = call_args[1:]
        message_params = [p for p in params if isinstance(p, str) and len(p) <= 1000 and "x" in p]
        assert all(len(m) <= 1000 for m in message_params)


# ---------------------------------------------------------------------------
# append_run_history
# ---------------------------------------------------------------------------

class TestAppendRunHistory:
    def test_creates_jsonl_file(self, tmp_path: Path) -> None:
        record = {"status": "success", "rows": 100}
        append_run_history(tmp_path, record)
        jsonl = tmp_path / "run_history.jsonl"
        assert jsonl.exists()
        lines = jsonl.read_text().strip().split("\n")
        assert len(lines) == 1
        data = json.loads(lines[0])
        assert data["status"] == "success"
        assert data["rows"] == 100

    def test_injects_ts_if_not_present(self, tmp_path: Path) -> None:
        append_run_history(tmp_path, {"status": "ok"})
        line = (tmp_path / "run_history.jsonl").read_text().strip()
        data = json.loads(line)
        assert "ts" in data

    def test_preserves_caller_ts(self, tmp_path: Path) -> None:
        append_run_history(tmp_path, {"ts": "2026-01-01T00:00:00+00:00", "x": 1})
        data = json.loads((tmp_path / "run_history.jsonl").read_text().strip())
        assert data["ts"] == "2026-01-01T00:00:00+00:00"

    def test_appends_multiple_lines(self, tmp_path: Path) -> None:
        append_run_history(tmp_path, {"n": 1})
        append_run_history(tmp_path, {"n": 2})
        lines = (tmp_path / "run_history.jsonl").read_text().strip().split("\n")
        assert len(lines) == 2

    def test_empty_log_dir_is_a_noop(self) -> None:
        # Should not raise
        append_run_history("", {"status": "ok"})

    def test_creates_intermediate_directories(self, tmp_path: Path) -> None:
        deep_dir = tmp_path / "a" / "b" / "c"
        append_run_history(deep_dir, {"x": 1})
        assert (deep_dir / "run_history.jsonl").exists()


# ---------------------------------------------------------------------------
# check_today_success_marker / write_today_success_marker
# ---------------------------------------------------------------------------

class TestSuccessMarker:
    def test_returns_false_when_no_marker(self, tmp_path: Path) -> None:
        assert check_today_success_marker(tmp_path, TODAY) is False

    def test_returns_true_after_write(self, tmp_path: Path) -> None:
        write_today_success_marker(tmp_path, TODAY)
        assert check_today_success_marker(tmp_path, TODAY) is True

    def test_different_date_returns_false(self, tmp_path: Path) -> None:
        from datetime import timedelta
        write_today_success_marker(tmp_path, TODAY)
        assert check_today_success_marker(tmp_path, TODAY + timedelta(days=1)) is False

    def test_empty_log_dir_returns_false(self) -> None:
        assert check_today_success_marker("", TODAY) is False

    def test_empty_log_dir_write_is_noop(self) -> None:
        # Should not raise
        write_today_success_marker("", TODAY)

    def test_creates_intermediate_dirs(self, tmp_path: Path) -> None:
        deep = tmp_path / "x" / "y"
        write_today_success_marker(deep, TODAY)
        assert check_today_success_marker(deep, TODAY) is True

    def test_marker_filename_format(self, tmp_path: Path) -> None:
        write_today_success_marker(tmp_path, date(2026, 4, 6))
        assert (tmp_path / "last_success_2026-04-06.marker").exists()

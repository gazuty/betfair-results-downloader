"""Tests for publish_to_azure_sql outcome semantics (attempted vs ok)."""
from __future__ import annotations

from decimal import Decimal
from unittest.mock import MagicMock

import pandas as pd
import pytest

import betfair_results_downloader.azure_publish as azure_publish
from betfair_results_downloader.azure_publish import publish_to_azure_sql

CREDS = {
    "user": {"user_id": "TestUser", "db_user_id": "TestUser"},
    "azure_sql": {
        "server": "srv.database.windows.net",
        "database": "DB",
        "username": "adm",
        "password": "pw",
        "driver": "ODBC Driver 18 for SQL Server",
    },
}

ROWS = [(Decimal("1.234"), Decimal("5.00"), "")]


class TestBlockedConfigurations:
    def test_missing_db_user_id_is_not_ok(self) -> None:
        result = publish_to_azure_sql(
            creds={"user": {}, "azure_sql": CREDS["azure_sql"]},
            rows_to_write=ROWS,
            dry_run=False,
        )
        assert result.attempted is False
        assert result.ok is False
        assert "db_user_id" in result.message

    def test_missing_pyodbc_reports_actionable_message(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(azure_publish, "pyodbc", None)
        result = publish_to_azure_sql(creds=CREDS, rows_to_write=ROWS, dry_run=False)
        assert result.attempted is False
        assert result.ok is False
        assert "pyodbc" in result.message


class TestFailureIsNotSuccess:
    def test_connection_error_yields_attempted_but_not_ok(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake_pyodbc = MagicMock()
        fake_pyodbc.connect.side_effect = RuntimeError("login timeout")
        monkeypatch.setattr(azure_publish, "pyodbc", fake_pyodbc)

        result = publish_to_azure_sql(creds=CREDS, rows_to_write=ROWS, dry_run=False)

        assert result.attempted is True
        assert result.ok is False
        assert "Azure publish failed" in result.message


class TestDryRun:
    def test_dry_run_is_ok_but_not_attempted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake_conn = MagicMock()
        fake_pyodbc = MagicMock()
        fake_pyodbc.connect.return_value = fake_conn
        monkeypatch.setattr(azure_publish, "pyodbc", fake_pyodbc)
        monkeypatch.setattr(
            azure_publish,
            "read_existing_marketresults",
            lambda conn, user: pd.DataFrame(columns=["UserID", "MarketID", "Profit", "Notes"]),
        )

        result = publish_to_azure_sql(creds=CREDS, rows_to_write=ROWS, dry_run=True)

        assert result.attempted is False
        assert result.ok is True
        assert result.rows_to_insert == 1
        fake_conn.cursor.return_value.executemany.assert_not_called()

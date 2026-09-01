"""
ODBC connection-string escaping.

ODBC treats ";" as the attribute separator and braces as quoting, so a
password containing either -- perfectly legal for Azure SQL -- would
truncate the connection string or leak its remainder into the next
attribute. Every configured value is brace-quoted with "}" doubled.
"""

from __future__ import annotations

from pathlib import Path

from betfair_results_downloader.azure_common import (
    DEFAULT_DRIVER,
    build_conn_str,
)


def _azsql(**overrides) -> dict:
    base = {
        "server": "myserver.database.windows.net",
        "database": "BettingResults",
        "username": "sqladmin",
        "password": "hunter2",
    }
    base.update(overrides)
    return base


def test_plain_values_are_brace_quoted() -> None:
    conn = build_conn_str(_azsql())
    assert "SERVER={myserver.database.windows.net,1433};" in conn
    assert "DATABASE={BettingResults};" in conn
    assert "UID={sqladmin};" in conn
    assert "PWD={hunter2};" in conn
    assert f"DRIVER={{{DEFAULT_DRIVER}}};" in conn


def test_semicolon_in_password_cannot_split_the_string() -> None:
    conn = build_conn_str(_azsql(password="p;w=d rest"))
    assert "PWD={p;w=d rest};" in conn


def test_closing_brace_in_password_is_doubled() -> None:
    conn = build_conn_str(_azsql(password="p}w{d"))
    assert "PWD={p}}w{d};" in conn


def test_driver_and_port_fall_back_to_defaults() -> None:
    conn = build_conn_str(_azsql())
    assert DEFAULT_DRIVER in conn
    assert ",1433};" in conn
    conn2 = build_conn_str(_azsql(driver="Custom Driver 1", port=14330))
    assert "DRIVER={Custom Driver 1};" in conn2
    assert ",14330};" in conn2


def test_scripts_use_the_shared_builder() -> None:
    """The duplicated builders in scripts/ are gone for good."""
    scripts_dir = Path(__file__).resolve().parents[1] / "scripts"
    for name in ("azure_create_schedulestate.py", "azure_upgrade_schedulestate.py"):
        text = (scripts_dir / name).read_text(encoding="utf-8")
        assert "_build_conn_str" not in text, f"{name} still has a local builder"
        assert (
            "from betfair_results_downloader.azure_common import build_conn_str" in text
        )

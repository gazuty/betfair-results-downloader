from __future__ import annotations

import os
import sys
from pathlib import Path

import pyodbc

_ROOT = Path(__file__).resolve().parents[1]
_SRC = _ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from betfair_results_downloader.secrets import credentials_path, get_nested, load_credentials


def _require(value: str | None, *, field: str) -> str:
    if not value:
        raise SystemExit(f"Missing required Azure SQL field: {field}")
    return value


def _ensure_credentials_file(path: Path) -> None:
    if not path.exists():
        raise SystemExit(f"Credentials file not found: {path}")


def _build_conn_str(azsql: dict) -> str:
    port = azsql.get("port", 1433)
    return (
        f"DRIVER={{{azsql['driver']}}};"
        f"SERVER={azsql['server']},{port};"
        f"DATABASE={azsql['database']};"
        f"UID={azsql['username']};"
        f"PWD={azsql['password']};"
        "Encrypt=yes;"
        "TrustServerCertificate=no;"
        "Connection Timeout=30;"
    )


def get_azure_connection() -> pyodbc.Connection:
    """
    Open a pyodbc connection using credentials loaded from credentials.json.
    """
    path = credentials_path()
    _ensure_credentials_file(path)

    creds = load_credentials(path)
    azsql = creds.get("azure_sql", None)
    if not azsql:
        raise SystemExit("Missing azure_sql block in credentials.json")

    server = _require(str(azsql.get("server") or "").strip(), field="azure_sql.server")
    database = _require(str(azsql.get("database") or "").strip(), field="azure_sql.database")
    username = _require(str(azsql.get("username") or "").strip(), field="azure_sql.username")
    password = _require(str(azsql.get("password") or "").strip(), field="azure_sql.password")
    driver = str(azsql.get("driver") or "ODBC Driver 18 for SQL Server").strip()

    azsql_norm = {
        "server": server,
        "database": database,
        "username": username,
        "password": password,
        "driver": driver,
        "port": azsql.get("port", 1433),
    }

    return pyodbc.connect(_build_conn_str(azsql_norm))


def get_scoped_user_id() -> str:
    env_user_id = (os.getenv("AZURE_SQL_USERID") or "").strip()
    if env_user_id:
        return env_user_id

    path = credentials_path()
    _ensure_credentials_file(path)
    creds = load_credentials(path)
    db_user_id = str(get_nested(creds, "user.db_user_id", "")).strip()
    if db_user_id:
        return db_user_id
    user_id = str(get_nested(creds, "user.user_id", "")).strip()
    if user_id:
        return user_id

    raise SystemExit("Missing AZURE_SQL_USERID and no user.db_user_id/user.user_id in credentials.json")

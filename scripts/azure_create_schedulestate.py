"""
azure_create_schedulestate.py
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Idempotent script that creates the ``dbo.ScheduleState`` table in the
configured Azure SQL database.  Safe to run repeatedly — uses
``IF OBJECT_ID(...) IS NULL`` to skip creation when the table already exists.

Usage::

    python scripts/azure_create_schedulestate.py

Credentials are loaded via the standard resolver (``secrets/credentials.location.json``
→ ``secrets/credentials.json``).  The Azure SQL connection details come from
``credentials.json["azure_sql"]``.
"""
from __future__ import annotations

import sys
from pathlib import Path

# Allow running directly without pip-installing the package
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from betfair_results_downloader.secrets import get_credentials_path, load_credentials


DDL = """\
IF OBJECT_ID(N'dbo.ScheduleState', N'U') IS NULL
BEGIN
    CREATE TABLE dbo.ScheduleState (
        UserID              NVARCHAR(64)   NOT NULL PRIMARY KEY,
        LastCoveredDateUtc  DATE           NULL,
        LastRunStartedUtc   DATETIME2(0)   NULL,
        LastRunFinishedUtc  DATETIME2(0)   NULL,
        LastRunStatus       NVARCHAR(16)   NULL,
        LastRunMessage      NVARCHAR(1000) NULL,
        UpdatedUtc          DATETIME2(0)   NOT NULL DEFAULT SYSUTCDATETIME()
    );
    PRINT 'Created dbo.ScheduleState';
END
ELSE
BEGIN
    PRINT 'dbo.ScheduleState already exists — no action taken';
END
"""


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


def main() -> int:
    creds_path = get_credentials_path()
    print(f"Credentials: {creds_path}")
    if not creds_path.exists():
        print(f"ERROR: credentials file not found at {creds_path}")
        return 1

    creds = load_credentials(creds_path)
    azsql = creds.get("azure_sql") or {}
    if not azsql.get("server"):
        print("ERROR: azure_sql.server not configured in credentials.")
        return 1

    try:
        import pyodbc
    except ImportError:
        print("ERROR: pyodbc not installed. Run: pip install pyodbc")
        return 1

    conn_str = _build_conn_str(azsql)
    print(f"Connecting to {azsql.get('server')}/{azsql.get('database')}...")
    try:
        with pyodbc.connect(conn_str, autocommit=True) as conn:
            cursor = conn.cursor()
            cursor.execute(DDL)
            while cursor.nextset():
                pass
            print("Done.")
    except Exception as e:
        print(f"ERROR: {type(e).__name__}: {e}")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())

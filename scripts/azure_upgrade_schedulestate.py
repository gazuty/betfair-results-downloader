"""
azure_upgrade_schedulestate.py
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Idempotent schema upgrade for ``dbo.ScheduleState``.

Adds intraday incremental checkpoint columns required by the timestamp-based
scheduler.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from betfair_results_downloader.secrets import get_credentials_path, load_credentials

# Each batch is executed separately: SQL Server compiles a batch before running
# it, so statements referencing columns added earlier in the same batch fail
# with "Invalid column name". DDL and the backfill UPDATEs must not share a batch.
DDL_BATCHES = [
    """\
IF OBJECT_ID(N'dbo.ScheduleState', N'U') IS NULL
BEGIN
    RAISERROR('dbo.ScheduleState does not exist. Run azure_create_schedulestate.py first.', 16, 1);
END

IF COL_LENGTH('dbo.ScheduleState', 'LastCoveredDateLocal') IS NULL
BEGIN
    ALTER TABLE dbo.ScheduleState
    ADD LastCoveredDateLocal DATE NULL;
    PRINT 'Added dbo.ScheduleState.LastCoveredDateLocal';
END
ELSE
BEGIN
    PRINT 'dbo.ScheduleState.LastCoveredDateLocal already exists';
END

IF COL_LENGTH('dbo.ScheduleState', 'LastCoveredTimezone') IS NULL
BEGIN
    ALTER TABLE dbo.ScheduleState
    ADD LastCoveredTimezone NVARCHAR(64) NULL;
    PRINT 'Added dbo.ScheduleState.LastCoveredTimezone';
END
ELSE
BEGIN
    PRINT 'dbo.ScheduleState.LastCoveredTimezone already exists';
END

IF COL_LENGTH('dbo.ScheduleState', 'LastConfirmedSettledAtUtc') IS NULL
BEGIN
    ALTER TABLE dbo.ScheduleState
    ADD LastConfirmedSettledAtUtc DATETIME2(0) NULL;
    PRINT 'Added dbo.ScheduleState.LastConfirmedSettledAtUtc';
END
ELSE
BEGIN
    PRINT 'dbo.ScheduleState.LastConfirmedSettledAtUtc already exists';
END

IF COL_LENGTH('dbo.ScheduleState', 'LastSuccessfulDownloadStartedUtc') IS NULL
BEGIN
    ALTER TABLE dbo.ScheduleState
    ADD LastSuccessfulDownloadStartedUtc DATETIME2(0) NULL;
    PRINT 'Added dbo.ScheduleState.LastSuccessfulDownloadStartedUtc';
END
ELSE
BEGIN
    PRINT 'dbo.ScheduleState.LastSuccessfulDownloadStartedUtc already exists';
END

IF COL_LENGTH('dbo.ScheduleState', 'LastSuccessfulDownloadFinishedUtc') IS NULL
BEGIN
    ALTER TABLE dbo.ScheduleState
    ADD LastSuccessfulDownloadFinishedUtc DATETIME2(0) NULL;
    PRINT 'Added dbo.ScheduleState.LastSuccessfulDownloadFinishedUtc';
END
ELSE
BEGIN
    PRINT 'dbo.ScheduleState.LastSuccessfulDownloadFinishedUtc already exists';
END
""",
    """\
UPDATE dbo.ScheduleState
SET LastCoveredDateLocal = LastCoveredDateUtc
WHERE LastCoveredDateUtc IS NOT NULL
  AND LastCoveredDateLocal IS NULL;

UPDATE dbo.ScheduleState
SET LastCoveredTimezone = 'Australia/Sydney'
WHERE LastCoveredDateLocal IS NOT NULL
  AND LastCoveredTimezone IS NULL;
""",
]


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
            for batch in DDL_BATCHES:
                cursor.execute(batch)
                while cursor.nextset():
                    pass
            print("Done.")
    except Exception as e:
        print(f"ERROR: {type(e).__name__}: {e}")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())

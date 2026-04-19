"""
scheduler/state.py
~~~~~~~~~~~~~~~~~~
Schedule state persistence layer (Phase 2.1).

Three storage mechanisms:

1. **Azure ``dbo.ScheduleState``** — remote source of truth for
   ``LastCoveredDateUtc``.  Requires ``user.enable_azure_sql=true`` and a
   working ``pyodbc`` connection.  Failures are caught and reported as
   warnings; the caller falls back to CSV-based gap detection.

2. **``run_history.jsonl``** — append-only local log of every run attempt
   (one JSON line per run).  Grows indefinitely; the caller decides whether
   to rotate it.

3. **``last_success_YYYY-MM-DD.marker``** — zero-byte touch file that signals
   the current calendar date has already been successfully covered.  Prevents
   multiple runs within the same day from re-downloading data.

All public functions are designed to be called from ``runner.py`` and
``gap_detector.py``; they never crash the caller on transient failures.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data container
# ---------------------------------------------------------------------------

@dataclass
class ScheduleStateRow:
    """One row from ``dbo.ScheduleState`` for the current user."""

    user_id: str
    last_covered_date_utc: Optional[date]
    last_run_started_utc: Optional[datetime]
    last_run_finished_utc: Optional[datetime]
    last_run_status: Optional[str]
    last_run_message: Optional[str]
    updated_utc: Optional[datetime]


# ---------------------------------------------------------------------------
# Azure helpers
# ---------------------------------------------------------------------------

def _get_db_user_id(creds: dict[str, Any]) -> str:
    """Return the Azure UserID for this user (db_user_id falls back to user_id)."""
    user = creds.get("user") or {}
    return str(user.get("db_user_id") or user.get("user_id") or "").strip()


def _build_conn_str(azsql: dict[str, Any]) -> str:
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


def _open_azure_connection(creds: dict[str, Any]):  # type: ignore[return]
    """
    Open a pyodbc connection using ``credentials["azure_sql"]``.

    Returns the connection object, or ``None`` if pyodbc is unavailable,
    Azure SQL is not configured, or the connection attempt fails.
    """
    azsql = creds.get("azure_sql") or {}
    if not azsql.get("server"):
        logger.debug("azure_sql.server not configured — skipping Azure state.")
        return None

    try:
        import pyodbc  # noqa: PLC0415
    except ImportError:
        logger.warning("pyodbc not installed — Azure ScheduleState unavailable.")
        return None

    try:
        conn_str = _build_conn_str(azsql)
        return pyodbc.connect(conn_str, autocommit=False)
    except Exception as exc:
        logger.warning("Azure connection failed: %s — falling back to CSV state.", exc)
        return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def read_schedule_state(creds: dict[str, Any]) -> Optional[ScheduleStateRow]:
    """
    Read the ``dbo.ScheduleState`` row for the current user from Azure SQL.

    Returns ``None`` if the row doesn't exist, Azure is unreachable, pyodbc is
    not installed, or the table doesn't exist yet.  Never raises.
    """
    user_id = _get_db_user_id(creds)
    if not user_id:
        logger.warning("Cannot read ScheduleState: db_user_id / user_id not set in credentials.")
        return None

    conn = _open_azure_connection(creds)
    if conn is None:
        return None

    try:
        with conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT UserID, LastCoveredDateUtc, LastRunStartedUtc, "
                "LastRunFinishedUtc, LastRunStatus, LastRunMessage, UpdatedUtc "
                "FROM dbo.ScheduleState WHERE UserID = ?",
                user_id,
            )
            row = cursor.fetchone()
            if row is None:
                return None
            return ScheduleStateRow(
                user_id=str(row[0]),
                last_covered_date_utc=row[1].date() if isinstance(row[1], datetime) else row[1],
                last_run_started_utc=row[2],
                last_run_finished_utc=row[3],
                last_run_status=str(row[4]) if row[4] is not None else None,
                last_run_message=str(row[5]) if row[5] is not None else None,
                updated_utc=row[6],
            )
    except Exception as exc:
        logger.warning("Failed to read ScheduleState from Azure: %s", exc)
        return None


def upsert_schedule_state(
    creds: dict[str, Any],
    last_covered_date: date,
    status: str,
    message: str,
    run_started_utc: Optional[datetime] = None,
    run_finished_utc: Optional[datetime] = None,
) -> bool:
    """
    MERGE/upsert the ``dbo.ScheduleState`` row for the current user.

    Parameters
    ----------
    creds:
        Full credentials dict.
    last_covered_date:
        The most recent date successfully covered by this run.
    status:
        Short status string — ``"success"``, ``"partial"``, or ``"failed"``.
    message:
        Human-readable summary (truncated to 1000 chars before writing).
    run_started_utc:
        UTC datetime the run started (defaults to now if not provided).
    run_finished_utc:
        UTC datetime the run finished (defaults to now if not provided).

    Returns
    -------
    bool
        ``True`` on success, ``False`` if upsert failed (caller logs the issue).
    """
    user_id = _get_db_user_id(creds)
    if not user_id:
        logger.warning("Cannot upsert ScheduleState: db_user_id / user_id not set.")
        return False

    conn = _open_azure_connection(creds)
    if conn is None:
        return False

    now_utc = datetime.now(timezone.utc).replace(tzinfo=None)  # SQL DATETIME2 is tz-naive
    started = (run_started_utc.replace(tzinfo=None) if run_started_utc else now_utc)
    finished = (run_finished_utc.replace(tzinfo=None) if run_finished_utc else now_utc)
    truncated_message = (message or "")[:1000]

    merge_sql = """\
MERGE dbo.ScheduleState AS target
USING (SELECT ? AS UserID) AS source ON target.UserID = source.UserID
WHEN MATCHED THEN
    UPDATE SET
        LastCoveredDateUtc  = ?,
        LastRunStartedUtc   = ?,
        LastRunFinishedUtc  = ?,
        LastRunStatus       = ?,
        LastRunMessage      = ?,
        UpdatedUtc          = SYSUTCDATETIME()
WHEN NOT MATCHED THEN
    INSERT (UserID, LastCoveredDateUtc, LastRunStartedUtc,
            LastRunFinishedUtc, LastRunStatus, LastRunMessage)
    VALUES (?, ?, ?, ?, ?, ?);
"""
    try:
        with conn:
            cursor = conn.cursor()
            cursor.execute(
                merge_sql,
                user_id,
                last_covered_date,
                started,
                finished,
                status[:16],
                truncated_message,
                user_id,
                last_covered_date,
                started,
                finished,
                status[:16],
                truncated_message,
            )
            conn.commit()
        return True
    except Exception as exc:
        logger.warning("Failed to upsert ScheduleState: %s", exc)
        return False


def append_run_history(log_dir: str | Path, run_record: dict[str, Any]) -> None:
    """
    Append one JSON line to ``run_history.jsonl`` in ``log_dir``.

    Creates the directory and file if they do not exist.  Silently skips on
    any IO error so it never crashes the caller.

    Parameters
    ----------
    log_dir:
        Directory where ``run_history.jsonl`` lives (or will be created).
    run_record:
        Arbitrary dict — serialised as a single JSON line.  A ``"ts"`` key
        (UTC ISO timestamp) is injected automatically if not already present.
    """
    if not log_dir:
        logger.debug("append_run_history: log_dir empty — skipping.")
        return

    try:
        log_path = Path(log_dir)
        log_path.mkdir(parents=True, exist_ok=True)
        record = dict(run_record)
        if "ts" not in record:
            record["ts"] = datetime.now(timezone.utc).isoformat()
        jsonl_path = log_path / "run_history.jsonl"
        with jsonl_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, default=str) + "\n")
    except Exception as exc:
        logger.warning("Failed to append to run_history.jsonl: %s", exc)


def check_today_success_marker(log_dir: str | Path, today_date: date) -> bool:
    """
    Check whether today's success marker file exists.

    The marker file is named ``last_success_YYYY-MM-DD.marker`` and lives
    in ``log_dir``.  Its presence means today's data has already been fully
    downloaded and processed.

    Returns ``False`` if ``log_dir`` is empty, the marker is absent, or any
    IO error occurs.
    """
    if not log_dir:
        return False
    try:
        marker = Path(log_dir) / f"last_success_{today_date.isoformat()}.marker"
        return marker.exists()
    except Exception as exc:
        logger.warning("Failed to check success marker: %s", exc)
        return False


def write_today_success_marker(log_dir: str | Path, today_date: date) -> None:
    """
    Write (touch) today's success marker file in ``log_dir``.

    Creates the directory if it does not exist.  Silently skips on any IO error.
    """
    if not log_dir:
        logger.debug("write_today_success_marker: log_dir empty — skipping.")
        return
    try:
        log_path = Path(log_dir)
        log_path.mkdir(parents=True, exist_ok=True)
        marker = log_path / f"last_success_{today_date.isoformat()}.marker"
        marker.touch()
    except Exception as exc:
        logger.warning("Failed to write success marker: %s", exc)

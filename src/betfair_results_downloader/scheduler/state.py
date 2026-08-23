"""
scheduler/state.py
~~~~~~~~~~~~~~~~~~
Schedule state persistence layer for scheduled downloader runs.

Storage mechanisms:

1. **Azure ``dbo.ScheduleState``** — remote source of truth for both day-level
   coverage fields and the latest confirmed settled timestamp checkpoint used by
   intraday incremental scheduling.

2. **``run_history.jsonl``** — append-only local log of every run attempt.

3. **Marker files** — zero-byte files used for lightweight operational audit.
   These are no longer used to suppress later intraday scheduled runs.

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

from ..azure_common import build_conn_str as _build_conn_str

logger = logging.getLogger(__name__)


@dataclass
class ScheduleStateRow:
    """One row from ``dbo.ScheduleState`` for the current user."""

    user_id: str
    last_covered_date_utc: Optional[date]
    last_covered_date_local: Optional[date]
    last_covered_timezone: Optional[str]
    last_confirmed_settled_at_utc: Optional[datetime]
    last_successful_download_started_utc: Optional[datetime]
    last_successful_download_finished_utc: Optional[datetime]
    last_run_started_utc: Optional[datetime]
    last_run_finished_utc: Optional[datetime]
    last_run_status: Optional[str]
    last_run_message: Optional[str]
    updated_utc: Optional[datetime]


def _get_db_user_id(creds: dict[str, Any]) -> str:
    user = creds.get("user") or {}
    return str(user.get("db_user_id") or user.get("user_id") or "").strip()


def _open_azure_connection(creds: dict[str, Any]):  # type: ignore[return]
    azsql = creds.get("azure_sql") or {}
    if not azsql.get("server"):
        logger.debug("azure_sql.server not configured, skipping Azure state.")
        return None

    try:
        import pyodbc  # noqa: PLC0415
    except ImportError:
        logger.warning("pyodbc not installed, Azure ScheduleState unavailable.")
        return None

    try:
        conn_str = _build_conn_str(azsql)
        return pyodbc.connect(conn_str, autocommit=False)
    except Exception as exc:
        logger.warning("Azure connection failed: %s, falling back to CSV state.", exc)
        return None


def _coerce_optional_datetime(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    return None


def read_schedule_state(creds: dict[str, Any]) -> Optional[ScheduleStateRow]:
    user_id = _get_db_user_id(creds)
    if not user_id:
        logger.warning(
            "Cannot read ScheduleState: db_user_id / user_id not set in credentials."
        )
        return None

    conn = _open_azure_connection(creds)
    if conn is None:
        return None

    try:
        with conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT UserID, LastCoveredDateUtc, LastCoveredDateLocal, LastCoveredTimezone, "
                "LastConfirmedSettledAtUtc, LastSuccessfulDownloadStartedUtc, LastSuccessfulDownloadFinishedUtc, "
                "LastRunStartedUtc, LastRunFinishedUtc, LastRunStatus, LastRunMessage, UpdatedUtc "
                "FROM dbo.ScheduleState WHERE UserID = ?",
                user_id,
            )
            row = cursor.fetchone()
            if row is None:
                return None
            return ScheduleStateRow(
                user_id=str(row[0]),
                last_covered_date_utc=row[1].date()
                if isinstance(row[1], datetime)
                else row[1],
                last_covered_date_local=row[2].date()
                if isinstance(row[2], datetime)
                else row[2],
                last_covered_timezone=str(row[3]) if row[3] is not None else None,
                last_confirmed_settled_at_utc=_coerce_optional_datetime(row[4]),
                last_successful_download_started_utc=_coerce_optional_datetime(row[5]),
                last_successful_download_finished_utc=_coerce_optional_datetime(row[6]),
                last_run_started_utc=_coerce_optional_datetime(row[7]),
                last_run_finished_utc=_coerce_optional_datetime(row[8]),
                last_run_status=str(row[9]) if row[9] is not None else None,
                last_run_message=str(row[10]) if row[10] is not None else None,
                updated_utc=_coerce_optional_datetime(row[11]),
            )
    except Exception as exc:
        logger.warning("Failed to read ScheduleState from Azure: %s", exc)
        return None


def upsert_schedule_state(
    creds: dict[str, Any],
    last_covered_date_utc: date,
    last_covered_date_local: date,
    last_covered_timezone: str,
    status: str,
    message: str,
    run_started_utc: Optional[datetime] = None,
    run_finished_utc: Optional[datetime] = None,
    last_confirmed_settled_at_utc: Optional[datetime] = None,
    last_successful_download_started_utc: Optional[datetime] = None,
    last_successful_download_finished_utc: Optional[datetime] = None,
) -> bool:
    user_id = _get_db_user_id(creds)
    if not user_id:
        logger.warning("Cannot upsert ScheduleState: db_user_id / user_id not set.")
        return False

    conn = _open_azure_connection(creds)
    if conn is None:
        return False

    now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
    started = (
        run_started_utc.astimezone(timezone.utc).replace(tzinfo=None)
        if run_started_utc
        else now_utc
    )
    finished = (
        run_finished_utc.astimezone(timezone.utc).replace(tzinfo=None)
        if run_finished_utc
        else now_utc
    )
    confirmed = (
        last_confirmed_settled_at_utc.astimezone(timezone.utc).replace(tzinfo=None)
        if last_confirmed_settled_at_utc
        else None
    )
    download_started = (
        last_successful_download_started_utc.astimezone(timezone.utc).replace(
            tzinfo=None
        )
        if last_successful_download_started_utc
        else None
    )
    download_finished = (
        last_successful_download_finished_utc.astimezone(timezone.utc).replace(
            tzinfo=None
        )
        if last_successful_download_finished_utc
        else None
    )
    truncated_message = (message or "")[:1000]

    # WITH (HOLDLOCK): serialize concurrent MERGEs from two machines so the
    # classic MERGE upsert race cannot raise a duplicate-key error.
    # LastConfirmedSettledAtUtc is monotonic non-decreasing: a NULL parameter
    # keeps the stored checkpoint (e.g. an empty download confirmed nothing),
    # and an older value never rewinds it.
    merge_sql = """\
MERGE dbo.ScheduleState WITH (HOLDLOCK) AS target
USING (SELECT ? AS UserID) AS source ON target.UserID = source.UserID
WHEN MATCHED THEN
    UPDATE SET
        LastCoveredDateUtc                 = ?,
        LastCoveredDateLocal               = ?,
        LastCoveredTimezone                = ?,
        LastConfirmedSettledAtUtc          = CASE
            WHEN ? IS NULL THEN target.LastConfirmedSettledAtUtc
            WHEN target.LastConfirmedSettledAtUtc IS NULL
                 OR ? > target.LastConfirmedSettledAtUtc THEN ?
            ELSE target.LastConfirmedSettledAtUtc
        END,
        LastSuccessfulDownloadStartedUtc   = ?,
        LastSuccessfulDownloadFinishedUtc  = ?,
        LastRunStartedUtc                  = ?,
        LastRunFinishedUtc                 = ?,
        LastRunStatus                      = ?,
        LastRunMessage                     = ?,
        UpdatedUtc                         = SYSUTCDATETIME()
WHEN NOT MATCHED THEN
    INSERT (UserID, LastCoveredDateUtc, LastCoveredDateLocal, LastCoveredTimezone,
            LastConfirmedSettledAtUtc, LastSuccessfulDownloadStartedUtc, LastSuccessfulDownloadFinishedUtc,
            LastRunStartedUtc, LastRunFinishedUtc, LastRunStatus, LastRunMessage)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
"""
    try:
        with conn:
            cursor = conn.cursor()
            cursor.execute(
                merge_sql,
                user_id,
                last_covered_date_utc,
                last_covered_date_local,
                last_covered_timezone,
                confirmed,
                confirmed,
                confirmed,
                download_started,
                download_finished,
                started,
                finished,
                status[:16],
                truncated_message,
                user_id,
                last_covered_date_utc,
                last_covered_date_local,
                last_covered_timezone,
                confirmed,
                download_started,
                download_finished,
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
    if not log_dir:
        logger.debug("append_run_history: log_dir empty, skipping.")
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


def _marker_filename(marker_date: date, marker_namespace: str = "local") -> str:
    return f"last_success_{marker_namespace}_{marker_date.isoformat()}.marker"


def write_today_success_marker(
    log_dir: str | Path, today_date: date, marker_namespace: str = "local"
) -> None:
    if not log_dir:
        logger.debug("write_today_success_marker: log_dir empty, skipping.")
        return
    try:
        log_path = Path(log_dir)
        log_path.mkdir(parents=True, exist_ok=True)
        marker = log_path / _marker_filename(today_date, marker_namespace)
        marker.touch(exist_ok=True)
    except Exception as exc:
        logger.warning("Failed to write success marker: %s", exc)

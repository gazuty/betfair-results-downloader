from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, List, Tuple, Optional, Set

from decimal import Decimal

import pyodbc


@dataclass
class AzurePublishResult:
    attempted: bool
    inserted_rows: int = 0
    deleted_rows: int = 0
    message: str = ""


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


def _normalize_market_id(value: Any) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value).strip())
    except Exception:
        return None


def fetch_existing_market_ids(
    *,
    creds: dict[str, Any],
) -> Set[Decimal]:
    user = creds.get("user", {}) or {}
    azsql = creds.get("azure_sql", None)

    db_user_id = (user.get("db_user_id") or "").strip() or None
    if db_user_id is None:
        return set()

    if not azsql:
        return set()

    conn = None
    cur = None
    try:
        conn = pyodbc.connect(_build_conn_str(azsql))
        cur = conn.cursor()
        cur.execute("SELECT MarketID FROM dbo.MarketResults WHERE LTRIM(RTRIM(UserID)) = ?;", (db_user_id.strip(),))
        rows = cur.fetchall()
        ids = {_normalize_market_id(r[0]) for r in rows}
        return {i for i in ids if i is not None}
    except Exception:
        return set()
    finally:
        if cur is not None:
            cur.close()
        if conn is not None:
            conn.close()


def publish_new_markets_to_azure_sql(
    *,
    creds: dict[str, Any],
    rows_to_write: Optional[List[Tuple[Decimal, Decimal, str]]],
    dry_run: bool,
) -> AzurePublishResult:
    """
    Insert-only Azure publisher:
    - gated by enable_azure_sql in caller
    - if dry_run True => skip
    - requires user.db_user_id and azure_sql block in credentials.json
    - inserts only (no deletes)
    """
    if dry_run:
        return AzurePublishResult(attempted=False, inserted_rows=0, deleted_rows=0, message="Dry-run: Azure publish skipped.")

    user = creds.get("user", {}) or {}
    azsql = creds.get("azure_sql", None)

    db_user_id = (user.get("db_user_id") or "").strip() or None
    if db_user_id is None:
        return AzurePublishResult(attempted=False, message="Azure publish blocked: user.db_user_id missing in secrets.")

    if not azsql:
        return AzurePublishResult(attempted=False, message="Azure publish blocked: azure_sql block missing in secrets.")

    if not rows_to_write:
        return AzurePublishResult(attempted=False, message="Azure publish blocked: rows_to_write is empty.")

    conn = None
    cur = None
    try:
        conn = pyodbc.connect(_build_conn_str(azsql))
        conn.autocommit = False
        cur = conn.cursor()

        rows_for_db = [(db_user_id, *row) for row in rows_to_write]

        cur.fast_executemany = True
        cur.executemany(
            "INSERT INTO dbo.MarketResults (UserID, MarketID, Profit, Notes) VALUES (?, ?, ?, ?);",
            rows_for_db,
        )

        inserted = len(rows_for_db)
        conn.commit()

        return AzurePublishResult(
            attempted=True,
            inserted_rows=inserted,
            deleted_rows=0,
            message=f"Azure publish complete: inserted={inserted:,} for UserID={db_user_id!r}.",
        )
    except Exception as e:
        if conn is not None:
            conn.rollback()
        return AzurePublishResult(attempted=True, inserted_rows=0, deleted_rows=0, message=f"Azure publish failed: {e}")
    finally:
        if cur is not None:
            cur.close()
        if conn is not None:
            conn.close()

def publish_to_azure_sql(
    *,
    creds: dict[str, Any],
    rows_to_write: Optional[List[Tuple[Decimal, Decimal, str]]],
    dry_run: bool,
) -> AzurePublishResult:
    """
    GUI branch Azure publisher (real implementation):
    - gated by enable_azure_sql in pipeline (not here)
    - if dry_run True => skip
    - requires user.db_user_id and azure_sql block in credentials.json
    - rebuild strategy: DELETE user rows then INSERT rows_to_write
    """
    if dry_run:
        return AzurePublishResult(attempted=False, inserted_rows=0, deleted_rows=0, message="Dry-run: Azure publish skipped.")

    user = creds.get("user", {}) or {}
    azsql = creds.get("azure_sql", None)

    db_user_id = (user.get("db_user_id") or "").strip() or None
    if db_user_id is None:
        return AzurePublishResult(attempted=False, message="Azure publish blocked: user.db_user_id missing in secrets.")

    if not azsql:
        return AzurePublishResult(attempted=False, message="Azure publish blocked: azure_sql block missing in secrets.")

    if not rows_to_write:
        return AzurePublishResult(attempted=False, message="Azure publish blocked: rows_to_write is empty.")

    conn = None
    cur = None
    try:
        conn = pyodbc.connect(_build_conn_str(azsql))
        conn.autocommit = False
        cur = conn.cursor()

        # Delete existing
        cur.execute("DELETE FROM dbo.MarketResults WHERE RTRIM(UserID) = ?;", (db_user_id,))
        deleted = cur.rowcount if cur.rowcount is not None else 0

        # Insert new
        rows_for_db = [(db_user_id, *row) for row in rows_to_write]

        cur.fast_executemany = True
        cur.executemany(
            "INSERT INTO dbo.MarketResults (UserID, MarketID, Profit, Notes) VALUES (?, ?, ?, ?);",
            rows_for_db,
        )

        inserted = len(rows_for_db)
        conn.commit()

        return AzurePublishResult(
            attempted=True,
            inserted_rows=inserted,
            deleted_rows=deleted,
            message=f"Azure publish complete: deleted={deleted:,}, inserted={inserted:,} for UserID={db_user_id!r}.",
        )
    except Exception as e:
        if conn is not None:
            conn.rollback()
        return AzurePublishResult(attempted=True, inserted_rows=0, deleted_rows=0, message=f"Azure publish failed: {e}")
    finally:
        if cur is not None:
            cur.close()
        if conn is not None:
            conn.close()

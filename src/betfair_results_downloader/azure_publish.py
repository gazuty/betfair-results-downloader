from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, List, Optional, Tuple

import pandas as pd
import pyodbc


@dataclass
class AzurePublishResult:
    attempted: bool
    inserted_rows: int = 0
    updated_rows: int = 0
    deleted_rows: int = 0
    existing_rows_in_db: int = 0
    matching_rows_unchanged: int = 0
    rows_to_update: int = 0
    rows_to_insert: int = 0
    rows_db_only_not_in_new: int = 0
    message: str = ""


@dataclass
class AzureSyncPlan:
    existing_count: int
    new_count: int
    unchanged_count: int
    update_count: int
    db_only_count: int
    rows_to_insert: list[tuple[Any, Any, Any, Any]]
    rows_to_update: list[tuple[Any, Any, Any, Any]]
    db_only_keys: list[str]


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


def _to_decimal(value: Any) -> Decimal | None:
    if value is None or pd.isna(value):
        return None
    try:
        return Decimal(str(value))
    except Exception:
        return None


def _normalize_key(value: Any) -> str:
    if value is None or pd.isna(value):
        return ""
    try:
        dec = Decimal(str(value))
    except Exception:
        return str(value).strip()
    if dec.is_nan():
        return ""
    if dec == dec.to_integral():
        return str(dec.to_integral())
    return format(dec.normalize(), "f").rstrip("0").rstrip(".")


def _profits_equal(a: Decimal | None, b: Decimal | None, tol: Decimal) -> bool:
    if isinstance(a, Decimal) and a.is_nan():
        a = None
    if isinstance(b, Decimal) and b.is_nan():
        b = None
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    return abs(a - b) <= tol


def read_existing_marketresults(
    conn: pyodbc.Connection, db_user_id: str
) -> pd.DataFrame:
    query = """
        SELECT RTRIM(UserID) AS UserID, MarketID, Profit, Notes
        FROM dbo.MarketResults
        WHERE RTRIM(UserID) = ?;
    """
    df = pd.read_sql_query(query, conn, params=[db_user_id])
    if df.empty:
        return pd.DataFrame(columns=["UserID", "MarketID", "Profit", "Notes"])
    return df


def _canonical_market_id(value: Any) -> str | None:
    key = _normalize_key(value)
    return key if key else None


def build_sync_plan(
    df_new: pd.DataFrame,
    df_existing: pd.DataFrame,
    *,
    profit_tolerance: Decimal = Decimal("0.000000001"),
) -> AzureSyncPlan:
    if df_existing is None or df_existing.empty:
        df_existing = pd.DataFrame(columns=["UserID", "MarketID", "Profit", "Notes"])

    df_new = df_new.copy()
    df_existing = df_existing.copy()

    df_new["MarketID_key"] = df_new["MarketID"].apply(_normalize_key)
    df_existing["MarketID_key"] = df_existing["MarketID"].apply(_normalize_key)

    df_new = df_new[df_new["MarketID_key"] != ""]
    df_existing = df_existing[df_existing["MarketID_key"] != ""]

    dupes_new = (
        df_new["MarketID_key"][df_new["MarketID_key"].duplicated()].unique().tolist()
    )
    if dupes_new:
        examples = ", ".join(dupes_new[:10])
        raise ValueError(f"Duplicate MarketID_key in new dataset: {examples}")

    dupes_existing = (
        df_existing["MarketID_key"][df_existing["MarketID_key"].duplicated()]
        .unique()
        .tolist()
    )
    if dupes_existing:
        examples = ", ".join(dupes_existing[:10])
        raise ValueError(f"Duplicate MarketID_key in existing dataset: {examples}")

    merged = df_new.merge(
        df_existing,
        on="MarketID_key",
        how="outer",
        suffixes=("_new", "_db"),
        indicator=True,
    )

    existing_count = len(df_existing)
    new_only = merged[merged["_merge"] == "left_only"]
    db_only = merged[merged["_merge"] == "right_only"]
    both = merged[merged["_merge"] == "both"].copy()

    if not both.empty:
        both["Profit_new_dec"] = both["Profit_new"].apply(_to_decimal)
        both["Profit_db_dec"] = both["Profit_db"].apply(_to_decimal)
        both["_profit_equal"] = both.apply(
            lambda r: _profits_equal(
                r["Profit_new_dec"], r["Profit_db_dec"], profit_tolerance
            ),
            axis=1,
        )
    else:
        both["_profit_equal"] = pd.Series(dtype="bool")

    unchanged_count = int(both["_profit_equal"].sum()) if not both.empty else 0
    update_count = len(both) - unchanged_count

    new_keys = set(new_only["MarketID_key"].tolist())
    update_keys = (
        set(both.loc[~both["_profit_equal"], "MarketID_key"].tolist())
        if not both.empty
        else set()
    )

    rows_to_insert_df = df_new[df_new["MarketID_key"].isin(new_keys)]
    rows_to_update_df = df_new[df_new["MarketID_key"].isin(update_keys)]

    rows_to_insert_df = rows_to_insert_df.copy()
    rows_to_update_df = rows_to_update_df.copy()
    rows_to_insert_df["MarketID_canon"] = rows_to_insert_df["MarketID"].apply(
        _canonical_market_id
    )
    rows_to_update_df["MarketID_canon"] = rows_to_update_df["MarketID"].apply(
        _canonical_market_id
    )
    rows_to_insert_df = rows_to_insert_df[rows_to_insert_df["MarketID_canon"].notna()]
    rows_to_update_df = rows_to_update_df[rows_to_update_df["MarketID_canon"].notna()]

    rows_to_insert = [
        (r["UserID"], r["MarketID_canon"], r["Profit"], r["Notes"])
        for _, r in rows_to_insert_df.iterrows()
    ]
    rows_to_update = [
        (r["Profit"], r["UserID"], r["MarketID_canon"])
        for _, r in rows_to_update_df.iterrows()
    ]

    return AzureSyncPlan(
        existing_count=existing_count,
        new_count=len(rows_to_insert),
        unchanged_count=unchanged_count,
        update_count=update_count,
        db_only_count=len(db_only),
        rows_to_insert=rows_to_insert,
        rows_to_update=rows_to_update,
        db_only_keys=sorted(set(db_only["MarketID_key"].tolist())),
    )


def apply_sync_plan(cur: pyodbc.Cursor, plan: AzureSyncPlan) -> tuple[int, int]:
    inserted = 0
    updated = 0

    if plan.rows_to_update:
        cur.fast_executemany = True
        cur.executemany(
            "UPDATE dbo.MarketResults SET Profit = ? WHERE RTRIM(UserID) = ? AND MarketID = ?;",
            plan.rows_to_update,
        )
        updated = len(plan.rows_to_update)

    if plan.rows_to_insert:
        cur.fast_executemany = True
        cur.executemany(
            "INSERT INTO dbo.MarketResults (UserID, MarketID, Profit, Notes) VALUES (?, ?, ?, ?);",
            plan.rows_to_insert,
        )
        inserted = len(plan.rows_to_insert)

    return inserted, updated


def publish_to_azure_sql(
    *,
    creds: dict[str, Any],
    rows_to_write: Optional[List[Tuple[Decimal, Decimal, str]]],
    dry_run: bool,
) -> AzurePublishResult:
    """
    GUI branch Azure publisher (real implementation):
    - gated by enable_azure_sql in pipeline (not here)
    - if dry_run True => skip DB writes
    - requires user.db_user_id and azure_sql block in credentials.json
    - incremental sync: read existing, compare, update/insert differences
    """
    user = creds.get("user", {}) or {}
    azsql = creds.get("azure_sql", None)

    db_user_id = (user.get("db_user_id") or "").strip() or None
    if db_user_id is None:
        return AzurePublishResult(
            attempted=False,
            message="Azure publish blocked: user.db_user_id missing in secrets.",
        )

    if not azsql:
        return AzurePublishResult(
            attempted=False,
            message="Azure publish blocked: azure_sql block missing in secrets.",
        )

    if not rows_to_write:
        return AzurePublishResult(
            attempted=False, message="Azure publish blocked: rows_to_write is empty."
        )

    conn = None
    cur = None
    try:
        conn = pyodbc.connect(_build_conn_str(azsql))
        conn.autocommit = False
        cur = conn.cursor()

        rows_for_db = [(db_user_id, *row) for row in rows_to_write]
        df_new = pd.DataFrame(
            rows_for_db, columns=["UserID", "MarketID", "Profit", "Notes"]
        )

        df_existing = read_existing_marketresults(conn, db_user_id)
        plan = build_sync_plan(df_new, df_existing)

        if dry_run:
            return AzurePublishResult(
                attempted=False,
                inserted_rows=0,
                updated_rows=0,
                deleted_rows=0,
                existing_rows_in_db=plan.existing_count,
                matching_rows_unchanged=plan.unchanged_count,
                rows_to_update=plan.update_count,
                rows_to_insert=plan.new_count,
                rows_db_only_not_in_new=plan.db_only_count,
                message="Dry run: no changes applied.",
            )

        inserted, updated = apply_sync_plan(cur, plan)
        conn.commit()

        return AzurePublishResult(
            attempted=True,
            inserted_rows=inserted,
            updated_rows=updated,
            deleted_rows=0,
            existing_rows_in_db=plan.existing_count,
            matching_rows_unchanged=plan.unchanged_count,
            rows_to_update=plan.update_count,
            rows_to_insert=plan.new_count,
            rows_db_only_not_in_new=plan.db_only_count,
            message=(
                "Azure publish complete: "
                f"existing={plan.existing_count:,}, unchanged={plan.unchanged_count:,}, "
                f"updated={updated:,}, inserted={inserted:,}, db_only={plan.db_only_count:,} "
                f"for UserID={db_user_id!r}."
            ),
        )
    except Exception as e:
        if conn is not None:
            conn.rollback()
        return AzurePublishResult(
            attempted=True,
            inserted_rows=0,
            updated_rows=0,
            deleted_rows=0,
            message=f"Azure publish failed: {e}",
        )
    finally:
        if cur is not None:
            cur.close()
        if conn is not None:
            conn.close()

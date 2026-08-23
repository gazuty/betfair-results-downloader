from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterable

try:
    import pyodbc  # type: ignore[import-not-found]
except Exception:  # pragma: no cover - environment-dependent native import
    pyodbc = None  # type: ignore[assignment]

if TYPE_CHECKING:
    import pyodbc as pyodbc_types

from .azure_common import build_conn_str as _build_conn_str
from .secrets import credentials_path, get_nested, load_credentials


@dataclass
class RowIdentifier:
    order_by_sql: str
    reason: str


def _ensure_credentials_file(path: Path) -> None:
    if not path.exists():
        raise SystemExit(f"Credentials file not found: {path}")


def _require(value: str | None, *, field: str) -> str:
    if not value:
        raise SystemExit(f"Missing required Azure SQL field: {field}")
    return value


def _load_creds() -> dict[str, Any]:
    path = credentials_path()
    _ensure_credentials_file(path)
    return load_credentials(path)


def get_scoped_user_id() -> str:
    creds = _load_creds()
    db_user_id = str(get_nested(creds, "user.db_user_id", "")).strip()
    if db_user_id:
        return db_user_id
    user_id = str(get_nested(creds, "user.user_id", "")).strip()
    if user_id:
        return user_id
    raise SystemExit("Missing user.db_user_id/user.user_id in credentials.json")


def get_azure_connection() -> "pyodbc_types.Connection":
    creds = _load_creds()
    azsql = creds.get("azure_sql", None)
    if not azsql:
        raise SystemExit("Missing azure_sql block in credentials.json")

    server = _require(str(azsql.get("server") or "").strip(), field="azure_sql.server")
    database = _require(
        str(azsql.get("database") or "").strip(), field="azure_sql.database"
    )
    username = _require(
        str(azsql.get("username") or "").strip(), field="azure_sql.username"
    )
    password = _require(
        str(azsql.get("password") or "").strip(), field="azure_sql.password"
    )
    driver = str(azsql.get("driver") or "ODBC Driver 18 for SQL Server").strip()

    azsql_norm = {
        "server": server,
        "database": database,
        "username": username,
        "password": password,
        "driver": driver,
        "port": azsql.get("port", 1433),
    }

    if pyodbc is None:
        raise SystemExit(
            "pyodbc is unavailable. Install pyodbc and unixODBC/ODBC Driver dependencies to use Azure remediation."
        )

    return pyodbc.connect(_build_conn_str(azsql_norm))


def _normalize_table(table: str) -> str:
    if "." not in table:
        table = f"dbo.{table}"
    parts = table.split(".", 1)
    return f"[{parts[0]}].[{parts[1]}]"


def _write_csv(
    path: Path, rows: Iterable[Iterable[object]], headers: list[str]
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(headers)
        writer.writerows(rows)


def _timestamp_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")


def audit_duplicates(user_id: str, table: str) -> dict[str, Any]:
    table_q = _normalize_table(table)
    with get_azure_connection() as conn:
        cur = conn.cursor()
        total_rows = cur.execute(
            f"SELECT COUNT(*) FROM {table_q} WHERE LTRIM(RTRIM(UserID)) = ?;",
            (user_id,),
        ).fetchone()[0]
        dup_rows = cur.execute(
            f"""
            SELECT MarketID, COUNT(*) AS Cnt
            FROM {table_q}
            WHERE LTRIM(RTRIM(UserID)) = ?
            GROUP BY MarketID
            HAVING COUNT(*) > 1
            ORDER BY COUNT(*) DESC;
            """,
            (user_id,),
        ).fetchall()
        dup_count = len(dup_rows)
        dup_rows_involved = sum(int(r[1]) for r in dup_rows)

    out_path = Path("outputs") / f"azure_duplicates_{user_id}_{_timestamp_utc()}.csv"
    _write_csv(out_path, [(r[0], r[1]) for r in dup_rows], ["MarketID", "Count"])

    return {
        "user_id": user_id,
        "table": table,
        "total_rows": int(total_rows),
        "duplicated_marketids": int(dup_count),
        "rows_involved_in_duplication": int(dup_rows_involved),
        "duplicates_csv": str(out_path),
    }


def backup_user_rows(user_id: str, table: str) -> dict[str, Any]:
    table_q = _normalize_table(table)
    with get_azure_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            f"SELECT * FROM {table_q} WHERE LTRIM(RTRIM(UserID)) = ?;",
            (user_id,),
        )
        rows = cur.fetchall()
        headers = [col[0] for col in cur.description]

    out_path = Path("outputs") / f"azure_backup_{user_id}_{_timestamp_utc()}.csv"
    _write_csv(out_path, rows, headers)

    return {
        "user_id": user_id,
        "table": table,
        "rows_exported": int(len(rows)),
        "backup_csv": str(out_path),
    }


def preview_normalize_userid(user_id: str, table: str) -> dict[str, Any]:
    table_q = _normalize_table(table)
    with get_azure_connection() as conn:
        cur = conn.cursor()
        affected = cur.execute(
            f"""
            SELECT COUNT(*)
            FROM {table_q}
            WHERE LTRIM(RTRIM(UserID)) = ?
              AND UserID <> LTRIM(RTRIM(UserID));
            """,
            (user_id,),
        ).fetchone()[0]
    return {
        "user_id": user_id,
        "table": table,
        "rows_with_padding": int(affected),
    }


def normalize_userid(user_id: str, table: str) -> dict[str, Any]:
    table_q = _normalize_table(table)
    with get_azure_connection() as conn:
        conn.autocommit = False
        cur = conn.cursor()
        try:
            preview = preview_normalize_userid(user_id, table)
            affected = preview["rows_with_padding"]
            if affected == 0:
                conn.rollback()
                return {
                    **preview,
                    "rows_updated": 0,
                    "message": "No normalization needed.",
                }

            cur.execute(
                f"""
                UPDATE {table_q}
                SET UserID = LTRIM(RTRIM(UserID))
                WHERE LTRIM(RTRIM(UserID)) = ?
                  AND UserID <> LTRIM(RTRIM(UserID));
                """,
                (user_id,),
            )
            updated = cur.rowcount if cur.rowcount is not None else 0
            conn.commit()
            return {
                **preview,
                "rows_updated": int(updated),
                "message": "Normalization committed.",
            }
        except Exception:
            conn.rollback()
            raise


def delete_user_rows(user_id: str, table: str) -> dict[str, Any]:
    table_q = _normalize_table(table)
    with get_azure_connection() as conn:
        conn.autocommit = False
        cur = conn.cursor()
        try:
            pre = cur.execute(
                f"SELECT COUNT(*) FROM {table_q} WHERE LTRIM(RTRIM(UserID)) = ?;",
                (user_id,),
            ).fetchone()[0]

            cur.execute(
                f"DELETE FROM {table_q} WHERE LTRIM(RTRIM(UserID)) = ?;",
                (user_id,),
            )
            deleted = cur.rowcount if cur.rowcount is not None else 0

            post = cur.execute(
                f"SELECT COUNT(*) FROM {table_q} WHERE LTRIM(RTRIM(UserID)) = ?;",
                (user_id,),
            ).fetchone()[0]

            if int(post) != 0:
                conn.rollback()
                raise SystemExit(
                    f"Post-delete count is not zero ({post}). Rolled back."
                )

            conn.commit()
            return {
                "user_id": user_id,
                "table": table,
                "pre_delete_rows": int(pre),
                "rows_deleted": int(deleted),
                "post_delete_rows": int(post),
                "message": "Delete committed.",
            }
        except Exception:
            conn.rollback()
            raise


def _count_duplicates(cur: pyodbc.Cursor, table_q: str, user_id: str) -> int:
    return cur.execute(
        f"""
        SELECT COUNT(*) FROM (
            SELECT MarketID
            FROM {table_q}
            WHERE LTRIM(RTRIM(UserID)) = ?
            GROUP BY MarketID
            HAVING COUNT(*) > 1
        ) d;
        """,
        (user_id,),
    ).fetchone()[0]


def check_raw_userid_variants(user_id: str, table: str) -> dict[str, Any]:
    table_q = _normalize_table(table)
    with get_azure_connection() as conn:
        cur = conn.cursor()
        rows = cur.execute(
            f"""
            SELECT UserID, LEN(UserID) AS LenUserID, COUNT(*) AS Rows
            FROM {table_q}
            WHERE LTRIM(RTRIM(UserID)) = ?
            GROUP BY UserID, LEN(UserID)
            ORDER BY LEN(UserID) DESC, Rows DESC;
            """,
            (user_id,),
        ).fetchall()
    variants = [{"user_id": r[0], "length": int(r[1]), "rows": int(r[2])} for r in rows]
    return {
        "user_id": user_id,
        "table": table,
        "variants": variants,
        "variant_count": len(variants),
    }


def check_global_duplicates(table: str, limit: int = 10) -> dict[str, Any]:
    table_q = _normalize_table(table)
    with get_azure_connection() as conn:
        cur = conn.cursor()
        rows = cur.execute(
            f"""
            SELECT TOP {int(limit)} UserID, MarketID, COUNT(*) AS cnt
            FROM {table_q}
            GROUP BY UserID, MarketID
            HAVING COUNT(*) > 1
            ORDER BY cnt DESC;
            """
        ).fetchall()
    items = [{"user_id": r[0], "market_id": r[1], "count": int(r[2])} for r in rows]
    return {"table": table, "duplicates": items, "duplicate_count": len(items)}


def _sanitize_index_suffix(value: str) -> str:
    safe = []
    for ch in value:
        if ch.isalnum() or ch == "_":
            safe.append(ch)
        else:
            safe.append("_")
    return "".join(safe) or "User"


def _sql_string_literal(value: str) -> str:
    return value.replace("'", "''")


def create_scoped_unique_index(
    user_id: str, table: str, index_name: str | None = None
) -> dict[str, Any]:
    table_q = _normalize_table(table)
    variants = check_raw_userid_variants(user_id, table)
    if variants["variant_count"] > 1:
        raise SystemExit(
            "Multiple raw UserID variants detected; normalize before creating index."
        )

    with get_azure_connection() as conn:
        cur = conn.cursor()
        dup_count = _count_duplicates(cur, table_q, user_id)
        if dup_count != 0:
            raise SystemExit(
                f"Duplicates still exist ({dup_count}). Refusing to create index."
            )

        default_index = f"UX_MarketResults_UserID_MarketID_{_sanitize_index_suffix(user_id.strip())}"
        index_name = index_name or default_index

        exists = cur.execute(
            """
            SELECT 1
            FROM sys.indexes
            WHERE name = ? AND object_id = OBJECT_ID(?);
            """,
            (index_name, table),
        ).fetchone()
        if exists:
            return {
                "user_id": user_id,
                "table": table,
                "index_name": index_name,
                "created": False,
                "message": "Index already exists.",
            }

        filter_user = user_id
        if variants["variant_count"] == 1:
            filter_user = str(variants["variants"][0]["user_id"])

        user_literal = _sql_string_literal(filter_user)
        cur.execute(
            f"""
            DECLARE @sql nvarchar(max) = N'
                CREATE UNIQUE INDEX [{index_name}]
                ON {table_q} (UserID, MarketID)
                WHERE UserID = N''{user_literal}'';
            ';
            EXEC sp_executesql @sql;
            """
        )
        conn.commit()
        return {
            "user_id": user_id,
            "table": table,
            "index_name": index_name,
            "created": True,
            "message": "Index created.",
        }


def detect_row_identifier(table: str) -> RowIdentifier | None:
    table_q = _normalize_table(table)
    schema, name = table_q.strip("[]").split("].[")
    obj_name = f"{schema}.{name}"

    with get_azure_connection() as conn:
        cur = conn.cursor()
        cols = cur.execute(
            """
            SELECT c.name, t.name AS type_name, c.is_identity
            FROM sys.columns c
            JOIN sys.types t ON c.user_type_id = t.user_type_id
            WHERE c.object_id = OBJECT_ID(?)
            ORDER BY c.column_id;
            """,
            (obj_name,),
        ).fetchall()

        if not cols:
            return None

        ts_candidates = {
            "updatedat",
            "modifiedat",
            "createdat",
            "insertedat",
            "lastupdated",
            "datecreated",
            "timecreated",
            "timestamp",
        }
        ts_types = {"datetime", "datetime2", "smalldatetime", "datetimeoffset", "date"}

        identity_col = None
        for name_col, _type_name, is_identity in cols:
            if is_identity:
                identity_col = name_col

        pk_rows = cur.execute(
            """
            SELECT c.name
            FROM sys.indexes i
            JOIN sys.index_columns ic ON i.object_id = ic.object_id AND i.index_id = ic.index_id
            JOIN sys.columns c ON ic.object_id = c.object_id AND ic.column_id = c.column_id
            WHERE i.is_primary_key = 1
              AND i.object_id = OBJECT_ID(?)
            ORDER BY ic.key_ordinal;
            """,
            (obj_name,),
        ).fetchall()
        pk_cols = [r[0] for r in pk_rows]

        ts_col = None
        for name_col, type_name, _ in cols:
            if name_col.lower() in ts_candidates and type_name.lower() in ts_types:
                ts_col = name_col
                break

    order_parts = []
    reason_parts = []
    if ts_col:
        order_parts.append(f"[{ts_col}] DESC")
        reason_parts.append(f"timestamp column {ts_col}")
    if identity_col:
        order_parts.append(f"[{identity_col}] DESC")
        reason_parts.append(f"identity column {identity_col}")
    elif pk_cols:
        order_parts.extend([f"[{c}] DESC" for c in pk_cols])
        reason_parts.append(f"primary key {', '.join(pk_cols)}")

    if not order_parts:
        return None

    return RowIdentifier(
        order_by_sql=", ".join(order_parts), reason=", ".join(reason_parts)
    )


def dedupe_user_marketid(user_id: str, table: str) -> dict[str, Any]:
    table_q = _normalize_table(table)
    identifier = detect_row_identifier(table)
    if identifier is None:
        raise SystemExit(
            "No safe row identifier found (no timestamp, identity, or primary key). Aborting."
        )

    with get_azure_connection() as conn:
        conn.autocommit = False
        cur = conn.cursor()
        try:
            dup_count = _count_duplicates(cur, table_q, user_id)
            if dup_count == 0:
                conn.rollback()
                return {
                    "user_id": user_id,
                    "table": table,
                    "duplicate_marketids": 0,
                    "rows_deleted": 0,
                    "message": "No duplicates found. Nothing to do.",
                    "row_identifier": identifier.reason,
                }

            delete_sql = f"""
            WITH dups AS (
                SELECT *,
                       ROW_NUMBER() OVER (
                           PARTITION BY LTRIM(RTRIM(UserID)), MarketID
                           ORDER BY {identifier.order_by_sql}
                       ) AS rn
                FROM {table_q}
                WHERE LTRIM(RTRIM(UserID)) = ?
            )
            DELETE FROM dups WHERE rn > 1;
            """
            cur.execute(delete_sql, (user_id,))
            deleted = cur.rowcount if cur.rowcount is not None else 0

            remaining = _count_duplicates(cur, table_q, user_id)
            if remaining != 0:
                conn.rollback()
                raise SystemExit(
                    f"Duplicates remain after delete ({remaining}). Rolled back."
                )

            conn.commit()
            return {
                "user_id": user_id,
                "table": table,
                "duplicate_marketids": int(dup_count),
                "rows_deleted": int(deleted),
                "message": "Deduplication committed.",
                "row_identifier": identifier.reason,
            }
        except Exception:
            conn.rollback()
            raise


def _index_exists(cur: pyodbc.Cursor, index_name: str, table: str) -> bool:
    return bool(
        cur.execute(
            """
            SELECT 1
            FROM sys.indexes
            WHERE name = ? AND object_id = OBJECT_ID(?);
            """,
            (index_name, table),
        ).fetchone()
    )


def create_unique_index(
    *,
    scope: str,
    user_id: str,
    table: str,
    index_name: str | None = None,
) -> dict[str, Any]:
    scope = scope.strip().lower()
    if scope not in {"scoped", "global"}:
        raise SystemExit("scope must be 'scoped' or 'global'.")

    if scope == "global":
        global_dupes = check_global_duplicates(table)
        if global_dupes["duplicate_count"] != 0:
            raise SystemExit(
                "Global duplicates exist. Refusing to create unique index."
            )

        default_index = "UX_MarketResults_UserID_MarketID"
        index_name = index_name or default_index
        table_q = _normalize_table(table)
        with get_azure_connection() as conn:
            cur = conn.cursor()
            if _index_exists(cur, index_name, table):
                return {
                    "scope": scope,
                    "user_id": user_id,
                    "table": table,
                    "index_name": index_name,
                    "created": False,
                    "message": "Index already exists.",
                }
            cur.execute(
                f"CREATE UNIQUE INDEX [{index_name}] ON {table_q} (UserID, MarketID);"
            )
            conn.commit()
            return {
                "scope": scope,
                "user_id": user_id,
                "table": table,
                "index_name": index_name,
                "created": True,
                "message": "Index created.",
            }

    return create_scoped_unique_index(user_id, table, index_name=index_name)

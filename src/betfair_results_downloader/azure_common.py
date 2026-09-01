"""
Shared Azure SQL connection-string builder.

Single source of truth for the ODBC connection options (encryption, trust,
timeout) used by ``azure_publish``, ``azure_remediation``,
``scheduler/state``, and the one-off scripts in ``scripts/``.
"""

from __future__ import annotations

from typing import Any

DEFAULT_DRIVER = "ODBC Driver 18 for SQL Server"
DEFAULT_PORT = 1433


def _quote(value: Any) -> str:
    """
    Brace-quote one ODBC connection-string value.

    ODBC treats ``;`` as the attribute separator and ``{}`` as the quoting
    mechanism, so a password like ``p;w}d`` spliced in raw would truncate
    the string or leak the remainder into the next attribute. Wrapping the
    value in braces with any ``}`` doubled makes every value safe verbatim.
    """
    return "{" + str(value).replace("}", "}}") + "}"


def build_conn_str(azsql: dict[str, Any]) -> str:
    """
    Build a pyodbc connection string from an ``azure_sql`` credentials block.

    Requires ``server``, ``database``, ``username``, and ``password``;
    ``driver`` and ``port`` fall back to defaults. Every configured value is
    brace-quoted, so passwords containing ``;``, ``}`` or spaces work.
    """
    driver = azsql.get("driver") or DEFAULT_DRIVER
    port = azsql.get("port", DEFAULT_PORT)
    server_with_port = f"{azsql['server']},{port}"
    return (
        f"DRIVER={_quote(driver)};"
        f"SERVER={_quote(server_with_port)};"
        f"DATABASE={_quote(azsql['database'])};"
        f"UID={_quote(azsql['username'])};"
        f"PWD={_quote(azsql['password'])};"
        "Encrypt=yes;"
        "TrustServerCertificate=no;"
        "Connection Timeout=30;"
    )

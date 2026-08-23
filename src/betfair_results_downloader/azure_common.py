"""
Shared Azure SQL connection-string builder.

Single source of truth for the ODBC connection options (encryption, trust,
timeout) used by ``azure_publish``, ``azure_remediation``, and
``scheduler/state``.
"""
from __future__ import annotations

from typing import Any

DEFAULT_DRIVER = "ODBC Driver 18 for SQL Server"
DEFAULT_PORT = 1433


def build_conn_str(azsql: dict[str, Any]) -> str:
    """
    Build a pyodbc connection string from an ``azure_sql`` credentials block.

    Requires ``server``, ``database``, ``username``, and ``password``;
    ``driver`` and ``port`` fall back to defaults.
    """
    driver = azsql.get("driver") or DEFAULT_DRIVER
    port = azsql.get("port", DEFAULT_PORT)
    return (
        f"DRIVER={{{driver}}};"
        f"SERVER={azsql['server']},{port};"
        f"DATABASE={azsql['database']};"
        f"UID={azsql['username']};"
        f"PWD={azsql['password']};"
        "Encrypt=yes;"
        "TrustServerCertificate=no;"
        "Connection Timeout=30;"
    )

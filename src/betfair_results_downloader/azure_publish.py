from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class AzurePublishResult:
    attempted: bool
    inserted_rows: int = 0
    message: str = ""


def publish_to_azure_sql(*, creds: dict[str, Any], df: Any, dry_run: bool) -> AzurePublishResult:
    """
    Stub for GUI branch. Keep safe-by-default.
    Later we will plug in your proven Azure SQL logic here.

    df is intentionally typed as Any so the core pipeline can pass a pandas DataFrame
    without this module importing pandas.
    """
    if dry_run:
        return AzurePublishResult(attempted=False, inserted_rows=0, message="Dry-run: Azure publish skipped.")

    # When we implement: validate creds, connect via pyodbc, upsert/dedupe, etc.
    raise NotImplementedError("Azure publish is not implemented yet on feature/gui.")

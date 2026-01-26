from __future__ import annotations

import os

from _bootstrap import ensure_src_on_path

ensure_src_on_path()

from betfair_results_downloader.azure_remediation import (
    create_unique_index,
    get_scoped_user_id,
)


def main() -> None:
    table_raw = os.getenv("AZURE_SQL_TABLE") or "dbo.MarketResults"
    user_id = (os.getenv("AZURE_SQL_USERID") or "").strip() or get_scoped_user_id()
    scope = os.getenv("AZURE_SQL_INDEX_SCOPE") or "scoped"
    index_name = os.getenv("AZURE_SQL_UNIQUE_INDEX_NAME")

    summary = create_unique_index(
        scope=scope, user_id=user_id, table=table_raw, index_name=index_name
    )

    print(f"Scope: {summary['scope']}")
    print(f"UserID: {summary['user_id']}")
    print(f"Table: {summary['table']}")
    print(f"Index: {summary['index_name']}")
    print(summary["message"])


if __name__ == "__main__":
    main()

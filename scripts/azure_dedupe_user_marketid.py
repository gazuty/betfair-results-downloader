from __future__ import annotations

import os

from _bootstrap import ensure_src_on_path

ensure_src_on_path()

from betfair_results_downloader.azure_remediation import dedupe_user_marketid, get_scoped_user_id


def main() -> None:
    table_raw = os.getenv("AZURE_SQL_TABLE") or "dbo.MarketResults"
    user_id = (os.getenv("AZURE_SQL_USERID") or "").strip() or get_scoped_user_id()

    summary = dedupe_user_marketid(user_id, table_raw)

    print(f"Using row identifier: {summary['row_identifier']}")
    print(f"Duplicate MarketIDs: {summary['duplicate_marketids']}")
    print(f"Rows deleted: {summary['rows_deleted']}")
    print(summary["message"])


if __name__ == "__main__":
    main()

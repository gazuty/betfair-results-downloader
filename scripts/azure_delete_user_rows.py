from __future__ import annotations

import os

from _bootstrap import ensure_src_on_path

ensure_src_on_path()

from betfair_results_downloader.azure_remediation import delete_user_rows, get_scoped_user_id


def main() -> None:
    table_raw = os.getenv("AZURE_SQL_TABLE") or "dbo.MarketResults"
    user_id = (os.getenv("AZURE_SQL_USERID") or "").strip() or get_scoped_user_id()

    summary = delete_user_rows(user_id, table_raw)

    print(f"UserID: {summary['user_id']}")
    print(f"Table: {summary['table']}")
    print(f"Pre-delete rows: {summary['pre_delete_rows']}")
    print(f"Rows deleted: {summary['rows_deleted']}")
    print(f"Post-delete rows: {summary['post_delete_rows']}")
    print(summary["message"])


if __name__ == "__main__":
    main()

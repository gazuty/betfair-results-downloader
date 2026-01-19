from __future__ import annotations

import os

from _bootstrap import ensure_src_on_path

ensure_src_on_path()

from betfair_results_downloader.azure_remediation import audit_duplicates, get_scoped_user_id


def main() -> None:
    table_raw = os.getenv("AZURE_SQL_TABLE") or "dbo.MarketResults"
    user_id = (os.getenv("AZURE_SQL_USERID") or "").strip() or get_scoped_user_id()

    summary = audit_duplicates(user_id, table_raw)

    print(f"UserID: {summary['user_id']}")
    print(f"Table: {summary['table']}")
    print(f"Total rows: {summary['total_rows']}")
    print(f"Duplicated MarketIDs: {summary['duplicated_marketids']}")
    print(f"Rows involved in duplication: {summary['rows_involved_in_duplication']}")
    print(f"Wrote: {summary['duplicates_csv']}")


if __name__ == "__main__":
    main()

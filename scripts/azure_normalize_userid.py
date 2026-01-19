from __future__ import annotations

import os

from _bootstrap import ensure_src_on_path

ensure_src_on_path()

from betfair_results_downloader.azure_remediation import get_scoped_user_id, normalize_userid, preview_normalize_userid


def main() -> None:
    table_raw = os.getenv("AZURE_SQL_TABLE") or "dbo.MarketResults"
    user_id = (os.getenv("AZURE_SQL_USERID") or "").strip() or get_scoped_user_id()

    preview = preview_normalize_userid(user_id, table_raw)
    print(f"[NORMALIZE] Table: {preview['table']}")
    print(f"[NORMALIZE] UserID (trimmed): {preview['user_id']!r}")
    print(f"[NORMALIZE] Rows with padded UserID to fix: {preview['rows_with_padding']}")

    summary = normalize_userid(user_id, table_raw)
    print(f"[NORMALIZE] Rows updated: {summary['rows_updated']}")
    print(f"[NORMALIZE] {summary['message']}")


if __name__ == "__main__":
    main()

from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Callable, Optional

import pandas as pd

from .config import DownloaderConfig
from .downloader_core import prepare_azure_dataset
from .pipeline import run_pipeline
from .azure_publish import fetch_existing_market_ids, publish_new_markets_to_azure_sql
from .secrets import validate_credentials


def run_downloader(
    config: DownloaderConfig,
    creds: dict[str, Any],
    *,
    status_cb: Optional[Callable[[str], None]] = None,
    confirm_publish_cb: Optional[Callable[[dict[str, Any]], bool]] = None,
    last_settled_date_utc: Optional[date] = None,
    recommended_days: Optional[int] = None,
    recommendation_note: Optional[str] = None,
) -> dict[str, Any]:
    """
    GUI/CLI entrypoint:
    - validates config + creds
    - delegates to pipeline
    """
    config.validate()

    v = validate_credentials(creds)
    if not v.ok:
        raise ValueError("Invalid credentials:\n- " + "\n- ".join(v.errors))

    return run_pipeline(
        config=config,
        creds=creds,
        status_cb=status_cb,
        confirm_publish_cb=confirm_publish_cb,
        last_settled_date_utc=last_settled_date_utc,
        recommended_days=recommended_days,
        recommendation_note=recommendation_note,
    )


def publish_to_azure_from_canonical(
    config: DownloaderConfig,
    creds: dict[str, Any],
    *,
    status_cb: Optional[Callable[[str], None]] = None,
    confirm_publish_cb: Optional[Callable[[dict[str, Any]], bool]] = None,
) -> dict[str, Any]:
    def say(msg: str) -> None:
        if status_cb:
            status_cb(msg)

    summary_base = {
        "canonical_path": None,
        "canonical_rows_read": 0,
        "markets_aggregated": 0,
        "existing_markets_in_azure_count": 0,
        "new_markets_to_publish": 0,
        "publish_requested": True,
        "publish_attempted": False,
        "inserted_rows": 0,
        "message": "",
    }

    paths = creds.get("paths", {}) or {}
    results_dir_raw = paths.get("results_csv_dir")
    if not results_dir_raw:
        return {
            "ok": False,
            "message": "Missing paths.results_csv_dir in secrets/credentials.json",
            "summary": {**summary_base, "message": "Missing paths.results_csv_dir in secrets/credentials.json"},
        }

    canonical_path = Path(results_dir_raw) / "cleared_orders_cleaned.csv"
    if not canonical_path.exists():
        return {
            "ok": False,
            "message": f"Canonical CSV not found: {canonical_path}",
            "summary": {
                **summary_base,
                "canonical_path": str(canonical_path),
                "message": f"Canonical CSV not found: {canonical_path}",
            },
        }

    if not config.enable_azure_sql:
        return {
            "ok": False,
            "message": "Azure upload disabled (Enable Azure upload is unchecked).",
            "summary": {
                **summary_base,
                "canonical_path": str(canonical_path),
                "message": "Azure upload disabled (Enable Azure upload is unchecked).",
            },
        }

    v = validate_credentials(creds)
    if not v.ok:
        return {
            "ok": False,
            "message": "Invalid credentials:\n- " + "\n- ".join(v.errors),
            "summary": {
                **summary_base,
                "canonical_path": str(canonical_path),
                "message": "Invalid credentials:\n- " + "\n- ".join(v.errors),
            },
        }

    user = creds.get("user", {}) or {}
    db_user_id = (user.get("db_user_id") or "").strip() or None
    if db_user_id is None:
        return {
            "ok": False,
            "message": "Azure publish blocked: user.db_user_id missing in secrets.",
            "summary": {
                **summary_base,
                "canonical_path": str(canonical_path),
                "message": "Azure publish blocked: user.db_user_id missing in secrets.",
            },
        }

    say("Starting Azure publish-only...")

    try:
        df_co = pd.read_csv(canonical_path, dtype={"marketId": "string"}, low_memory=False)
    except Exception as e:
        return {
            "ok": False,
            "message": f"Failed to read canonical CSV: {e}",
            "summary": {
                **summary_base,
                "canonical_path": str(canonical_path),
                "message": f"Failed to read canonical CSV: {e}",
            },
        }

    canonical_rows = len(df_co)
    prep = prepare_azure_dataset(df_co=df_co, allowed_event_type_ids={7, 4339})
    df_market_results = prep.df_market_results
    markets_aggregated = prep.markets_aggregated

    if df_market_results is None or df_market_results.empty:
        say("Publish skipped: Azure already contains all markets.")
        return {
            "ok": True,
            "summary": {
                **summary_base,
                "canonical_path": str(canonical_path),
                "canonical_rows_read": canonical_rows,
                "markets_aggregated": markets_aggregated,
                "message": "No new markets to publish (Azure is up to date).",
            },
        }

    df_market_results = df_market_results.copy()

    def _to_decimal(value: str) -> Decimal | None:
        v = str(value).strip()
        if not v or v.lower() == "nan":
            return None
        try:
            return Decimal(v)
        except InvalidOperation:
            return None

    df_market_results["marketId_dec"] = df_market_results["marketId"].astype(str).apply(_to_decimal)
    df_market_results = df_market_results.dropna(subset=["marketId_dec"])

    existing_market_ids = fetch_existing_market_ids(creds=creds)
    df_new = df_market_results.loc[~df_market_results["marketId_dec"].isin(existing_market_ids)]

    rows_new: list[tuple[Decimal, Decimal, str]] = []
    for _, r in df_new.iterrows():
        market_id = r["marketId_dec"]
        profit = Decimal(str(r["Profit"])).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        rows_new.append((market_id, profit, ""))

    overlap = int(df_market_results["marketId_dec"].isin(existing_market_ids).sum())
    say(
        f"Publish-only: aggregated_markets={markets_aggregated}, "
        f"existing_in_azure={len(existing_market_ids)}, "
        f"to_publish={len(rows_new)}, "
        f"overlap={overlap}"
    )
    if len(existing_market_ids) == markets_aggregated and overlap == 0:
        say("Publish-only: WARNING - marketId overlap is zero; possible type/precision mismatch.")

    if not rows_new:
        say("Publish skipped: Azure already contains all markets.")
        return {
            "ok": True,
            "summary": {
                **summary_base,
                "canonical_path": str(canonical_path),
                "canonical_rows_read": canonical_rows,
                "markets_aggregated": markets_aggregated,
                "existing_markets_in_azure_count": len(existing_market_ids),
                "message": "No new markets to publish (Azure is up to date).",
            },
        }

    if config.dry_run:
        say("Publish to Azure skipped because Dry Run is enabled.")
        return {
            "ok": True,
            "summary": {
                **summary_base,
                "canonical_path": str(canonical_path),
                "canonical_rows_read": canonical_rows,
                "markets_aggregated": markets_aggregated,
                "existing_markets_in_azure_count": len(existing_market_ids),
                "new_markets_to_publish": len(rows_new),
                "message": "Publish to Azure skipped because Dry Run is enabled.",
            },
        }

    if confirm_publish_cb:
        ok = confirm_publish_cb(
            {
                "user_id": config.user_id,
                "markets_aggregated": markets_aggregated,
                "rows_to_write_count": len(rows_new),
            }
        )
        if not ok:
            return {
                "ok": True,
                "summary": {
                    **summary_base,
                    "canonical_path": str(canonical_path),
                    "canonical_rows_read": canonical_rows,
                    "markets_aggregated": markets_aggregated,
                    "existing_markets_in_azure_count": len(existing_market_ids),
                    "new_markets_to_publish": len(rows_new),
                    "message": "Azure publish cancelled by user (non-dry-run).",
                },
            }

    az = publish_new_markets_to_azure_sql(
        creds=creds,
        rows_to_write=rows_new,
        dry_run=False,
    )

    return {
        "ok": True,
        "summary": {
            **summary_base,
            "canonical_path": str(canonical_path),
            "canonical_rows_read": canonical_rows,
            "markets_aggregated": markets_aggregated,
            "existing_markets_in_azure_count": len(existing_market_ids),
            "new_markets_to_publish": len(rows_new),
            "publish_attempted": bool(az.attempted),
            "inserted_rows": az.inserted_rows,
            "message": az.message or "Azure publish complete.",
        },
    }

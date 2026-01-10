from __future__ import annotations

from dataclasses import asdict
from typing import Any, Dict

from pathlib import Path

from .azure_publish import publish_to_azure_sql
from .config import DownloaderConfig
from .downloader_core import (
    fetch_cleared_orders_df,
    enrich_with_market_catalogue,
    prepare_azure_dataset,
    write_csv_outputs,
)


def run_pipeline(*, config: DownloaderConfig, creds: dict[str, Any]) -> dict[str, Any]:
    """
    GUI pipeline (wired):
    1) download cleared orders (Betfair)
    2) optional enrichment (market catalogue cache)
    3) write canonical + snapshot CSV
    4) optional Azure: filter->aggregate->rebuild
    """
    config.validate()

    plan = {
        "days": config.days,
        "event_type_ids": config.selected_event_type_ids(),
        "include_horses": config.include_horses,
        "include_greyhounds": config.include_greyhounds,
        "enable_azure_sql": config.enable_azure_sql,
        "dry_run": config.dry_run,
        "user_id": config.user_id,
    }

    betfair = creds.get("betfair", {}) or {}
    paths = creds.get("paths", {}) or {}

    results_dir_raw = paths.get("results_csv_dir")
    if not results_dir_raw:
        return {
            "ok": False,
            "message": "Missing paths.results_csv_dir in secrets/credentials.json",
            "plan": plan,
        }

    results_dir = Path(results_dir_raw)

    # Try to infer repo_root for outputs/ cache folder.
    # If running from src/ package, outputs folder relative to cwd is OK as fallback.
    repo_root = Path.cwd()

    # -------------------
    # 1) Download
    # -------------------
    dl = fetch_cleared_orders_df(betfair=betfair, lookback_days=int(config.days))
    download_summary: Dict[str, Any] = {
        "download_attempted": dl.attempted,
        "rows_downloaded": dl.rows_downloaded,
        "message": dl.message,
    }

    if not dl.attempted or dl.df_co is None or dl.df_co.empty:
        # Still return a clean result; GUI shows message.
        return {
            "ok": True,
            "message": "Run completed (GUI branch).",
            "plan": plan,
            "download": download_summary,
            "csv": None,
            "azure": {
                "attempted": False,
                "inserted_rows": 0,
                "deleted_rows": 0,
                "message": "Azure skipped because download produced no rows.",
            },
        }

    df_co = dl.df_co

    # -------------------
    # 2) Optional enrichment (you can toggle later via secrets; default ON here)
    # -------------------
    df_co, enr = enrich_with_market_catalogue(
        df_co=df_co,
        betfair=betfair,
        repo_root=repo_root,
        enable=True,
        use_cache=True,
        batch_size=50,
        sleep_seconds=0.20,
    )

    enrich_summary = asdict(enr)

    # -------------------
    # 3) CSV outputs
    # -------------------
    csvr = write_csv_outputs(df_co=df_co, results_csv_dir=results_dir)
    csv_summary = {
        "canonical_path": str(csvr.canonical_path),
        "snapshot_path": str(csvr.snapshot_path),
        "rows_in_canonical": csvr.rows_in_canonical,
        "message": csvr.message,
    }

    # -------------------
    # 4) Azure (optional)
    # -------------------
    if not config.enable_azure_sql:
        azure_result = {
            "attempted": False,
            "inserted_rows": 0,
            "deleted_rows": 0,
            "message": "Azure upload disabled (Enable Azure upload is unchecked).",
        }
    else:
        prep = prepare_azure_dataset(df_co=df_co, allowed_event_type_ids={7, 4339})
        azure_prep_summary = {
            "attempted": prep.attempted,
            "rows_after_filter": prep.rows_after_filter,
            "markets_aggregated": prep.markets_aggregated,
            "message": prep.message,
        }

        az = publish_to_azure_sql(
            creds=creds,
            rows_to_write=prep.rows_to_write,
            dry_run=config.dry_run,
        )
        azure_result = {**azure_prep_summary, **asdict(az)}

    return {
        "ok": True,
        "message": "Run completed (GUI branch).",
        "plan": plan,
        "download": download_summary,
        "enrich": enrich_summary,
        "csv": csv_summary,
        "azure": azure_result,
    }

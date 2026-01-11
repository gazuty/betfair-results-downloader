from __future__ import annotations

from dataclasses import asdict
from typing import Any, Callable, Dict, Optional
from pathlib import Path

from .azure_publish import publish_to_azure_sql
from .config import DownloaderConfig
from .downloader_core import (
    fetch_cleared_orders_df,
    enrich_with_market_catalogue,
    prepare_azure_dataset,
    write_csv_outputs,
)


def run_pipeline(
    *,
    config: DownloaderConfig,
    creds: dict[str, Any],
    status_cb: Optional[Callable[[str], None]] = None,
    confirm_publish_cb: Optional[Callable[[dict[str, Any]], bool]] = None,
) -> dict[str, Any]:
    """
    GUI pipeline (wired):
    1) download cleared orders (Betfair)
    2) optional enrichment (market catalogue cache)
    3) write canonical + snapshot CSV
    4) optional Azure: filter->aggregate->rebuild
    """
    config.validate()

    def say(msg: str) -> None:
        if status_cb:
            status_cb(msg)

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
    say("Phase 1/4: Downloading cleared orders…")
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
            "enrich": None,
            "csv": None,
            "azure": {
                "prep_attempted": False,
                "publish_attempted": False,
                "inserted_rows": 0,
                "deleted_rows": 0,
                "message": "Azure skipped because download produced no rows.",
            },
        }

    df_co = dl.df_co

    def _unique_markets(df) -> int:
        try:
            if "marketId" in df.columns:
                return int(df["marketId"].nunique(dropna=True))
        except Exception:
            pass
        return 0

    # -------------------
    # 2) Optional enrichment
    # -------------------
    say("Phase 2/4: Enriching markets (market catalogue + cache)…")
    rows_before_enrich = len(df_co)
    unique_markets_input = _unique_markets(df_co)

    df_co, enr = enrich_with_market_catalogue(
        df_co=df_co,
        betfair=betfair,
        repo_root=repo_root,
        enable=True,
        use_cache=True,
        batch_size=50,
        sleep_seconds=0.20,
        status_cb=say,
    )
    enrich_summary = asdict(enr)

    # --- Enrichment reporting clarity (computed fields; no refactor) ---
    rows_after_enrich = len(df_co)
    unique_markets_after = _unique_markets(df_co)

    markets_returned = int(enrich_summary.get("markets_returned", 0) or 0)
    cache_hits = int(enrich_summary.get("cache_hits", 0) or 0)

    # Pragmatic definition: markets enriched = markets present after join.
    unique_markets_enriched = unique_markets_after if unique_markets_after else unique_markets_input

    unique_markets_from_api = markets_returned
    unique_markets_from_cache = 0
    if cache_hits > 0:
        # If there were cache hits, infer at least some enrichment came from cache.
        unique_markets_from_cache = max(min(unique_markets_enriched - unique_markets_from_api, unique_markets_enriched), 0)

    if unique_markets_enriched == 0:
        enrichment_mode = "none"
    elif unique_markets_from_api > 0 and unique_markets_from_cache > 0:
        enrichment_mode = "cache+api"
    elif unique_markets_from_api > 0:
        enrichment_mode = "api_only"
    else:
        enrichment_mode = "cache_only"

    enrich_summary.update(
        {
            "rows_before_enrich": rows_before_enrich,
            "rows_after_enrich": rows_after_enrich,
            "unique_markets_input": unique_markets_input,
            "unique_markets_enriched": unique_markets_enriched,
            "unique_markets_from_api": unique_markets_from_api,
            "unique_markets_from_cache": unique_markets_from_cache,
            "enrichment_mode": enrichment_mode,
        }
    )

    # -------------------
    # 3) CSV outputs
    # -------------------
    say("Phase 3/4: Writing CSV outputs…")
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
            "prep_attempted": False,
            "publish_attempted": False,
            "inserted_rows": 0,
            "deleted_rows": 0,
            "message": "Azure upload disabled (Enable Azure upload is unchecked).",
        }
    else:
        say("Phase 4/4: Preparing Azure dataset (filter + aggregate)…")
        prep = prepare_azure_dataset(df_co=df_co, allowed_event_type_ids={7, 4339})
        rows_to_write_count = len(prep.rows_to_write) if prep.rows_to_write else 0

        azure_prep_summary = {
            "prep_attempted": prep.attempted,
            "rows_after_filter": prep.rows_after_filter,
            "markets_aggregated": prep.markets_aggregated,
            "rows_to_write_count": rows_to_write_count,
            "message": prep.message,
        }

        # If non-dry-run, require explicit confirmation *after* we know what we're about to write.
        if (not config.dry_run) and confirm_publish_cb:
            say("Azure publish is armed (non-dry-run). Waiting for confirmation…")
            ok = confirm_publish_cb(
                {
                    **azure_prep_summary,
                    "user_id": config.user_id,
                }
            )
            if not ok:
                azure_result = {
                    **azure_prep_summary,
                    "publish_attempted": False,
                    "inserted_rows": 0,
                    "deleted_rows": 0,
                    "message": "Azure publish cancelled by user (non-dry-run).",
                }
                return {
                    "ok": True,
                    "message": "Run completed (GUI branch).",
                    "plan": plan,
                    "download": download_summary,
                    "enrich": enrich_summary,
                    "csv": csv_summary,
                    "azure": azure_result,
                }

        say("Publishing to Azure SQL…" if (not config.dry_run) else "Dry-run: Azure publish will be skipped.")
        az = publish_to_azure_sql(
            creds=creds,
            rows_to_write=prep.rows_to_write,
            dry_run=config.dry_run,
        )

        az_dict = asdict(az)
        # Avoid prep/publish key collisions and make dry-run reporting truthful.
        publish_attempted = bool(az_dict.pop("attempted", False))

        azure_result = {
            **azure_prep_summary,
            "publish_attempted": publish_attempted,
            **az_dict,
        }

    say("Done.")
    return {
        "ok": True,
        "message": "Run completed (GUI branch).",
        "plan": plan,
        "download": download_summary,
        "enrich": enrich_summary,
        "csv": csv_summary,
        "azure": azure_result,
    }

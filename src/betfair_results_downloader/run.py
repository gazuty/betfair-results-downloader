from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import Any, Callable, Optional

import pandas as pd

from .azure_publish import publish_to_azure_sql
from .config import DownloaderConfig
from .downloader_core import prepare_azure_dataset
from .pipeline import run_pipeline
from .secrets import validate_credentials


def run_downloader(
    config: DownloaderConfig,
    creds: dict[str, Any],
    *,
    status_cb: Optional[Callable[[str], None]] = None,
    confirm_publish_cb: Optional[Callable[[dict[str, Any]], bool]] = None,
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
    )


def publish_to_azure_from_canonical_incremental(
    config: DownloaderConfig,
    creds: dict[str, Any],
    *,
    status_cb: Optional[Callable[[str], None]] = None,
    confirm_publish_cb: Optional[Callable[[dict[str, Any]], bool]] = None,
) -> dict[str, Any]:
    def say(msg: str) -> None:
        if status_cb:
            status_cb(msg)

    config.validate()

    publish_only = {
        "attempted": False,
        "canonical_path": None,
        "rows_loaded": 0,
        "message": "",
    }
    azure_result: dict[str, Any] = {
        "publish_attempted": False,
        "inserted_rows": 0,
        "updated_rows": 0,
        "deleted_rows": 0,
        "message": "",
    }

    if not config.enable_azure_sql:
        msg = "Azure upload disabled (Enable Azure upload is unchecked)."
        publish_only["message"] = msg
        azure_result["message"] = msg
        return {"ok": False, "message": msg, "publish_only": publish_only, "azure": azure_result}

    if config.dry_run:
        msg = "Dry run is enabled. Turn it off to publish to Azure."
        publish_only["message"] = msg
        azure_result["message"] = msg
        return {"ok": False, "message": msg, "publish_only": publish_only, "azure": azure_result}

    paths = creds.get("paths", {}) or {}
    results_dir_raw = paths.get("results_csv_dir")
    if not results_dir_raw:
        msg = "Missing paths.results_csv_dir in secrets/credentials.json."
        publish_only["message"] = msg
        azure_result["message"] = msg
        return {"ok": False, "message": msg, "publish_only": publish_only, "azure": azure_result}

    canonical_path = Path(results_dir_raw) / "cleared_orders_cleaned.csv"
    publish_only["canonical_path"] = str(canonical_path)
    if not canonical_path.exists():
        msg = "Canonical CSV not found; run downloader first."
        publish_only["message"] = msg
        azure_result["message"] = msg
        return {"ok": False, "message": msg, "publish_only": publish_only, "azure": azure_result}

    say("Publish-only: Loading canonical CSV...")
    try:
        df_co = pd.read_csv(canonical_path, low_memory=False)
    except Exception as e:
        msg = f"Failed to read canonical CSV: {e}"
        publish_only["message"] = msg
        azure_result["message"] = msg
        return {"ok": False, "message": msg, "publish_only": publish_only, "azure": azure_result}

    publish_only["rows_loaded"] = len(df_co)
    publish_only["attempted"] = True

    say("Publish-only: Preparing Azure dataset...")
    prep = prepare_azure_dataset(df_co=df_co, allowed_event_type_ids={7, 4339})
    rows_to_write_count = len(prep.rows_to_write) if prep.rows_to_write else 0

    azure_prep_summary = {
        "prep_attempted": prep.attempted,
        "rows_after_filter": prep.rows_after_filter,
        "markets_aggregated": prep.markets_aggregated,
        "rows_to_write_count": rows_to_write_count,
        "message": prep.message,
    }

    if not prep.attempted:
        msg = prep.message or "Azure prep failed."
        publish_only["message"] = msg
        azure_result = {**azure_prep_summary, "publish_attempted": False, "message": msg}
        return {"ok": False, "message": msg, "publish_only": publish_only, "azure": azure_result}

    if not prep.rows_to_write:
        msg = "No rows to publish (Azure prep produced 0 rows)."
        publish_only["message"] = msg
        azure_result = {**azure_prep_summary, "publish_attempted": False, "message": msg}
        return {"ok": True, "message": msg, "publish_only": publish_only, "azure": azure_result}

    if confirm_publish_cb:
        ok = confirm_publish_cb({**azure_prep_summary, "user_id": config.user_id})
        if not ok:
            msg = "Azure publish cancelled by user (non-dry-run)."
            publish_only["message"] = msg
            azure_result = {**azure_prep_summary, "publish_attempted": False, "message": msg}
            return {"ok": True, "message": msg, "publish_only": publish_only, "azure": azure_result}

    say("Publish-only: Syncing to Azure (incremental)...")
    az = publish_to_azure_sql(
        creds=creds,
        rows_to_write=prep.rows_to_write,
        dry_run=False,
    )
    az_dict = asdict(az)
    publish_attempted = bool(az_dict.pop("attempted", False))
    azure_result = {**azure_prep_summary, "publish_attempted": publish_attempted, **az_dict}
    publish_only["message"] = az.message or "Azure publish complete."

    return {
        "ok": True,
        "message": publish_only["message"],
        "publish_only": publish_only,
        "azure": azure_result,
    }

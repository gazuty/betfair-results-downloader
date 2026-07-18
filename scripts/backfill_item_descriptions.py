#!/usr/bin/env python3
"""
backfill_item_descriptions.py
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Re-download all settled orders with ``include_item_description=True`` to enrich
the canonical CSV with market names, venues, runner names, market types, etc.

Processes date ranges in 14-day chunks with fresh API sessions for robustness.
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
import traceback
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

# Add src to path so we can import the package
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from betfair_results_downloader.secrets import credentials_path, load_credentials
from betfair_results_downloader.paths import resolve_results_dir
from betfair_results_downloader.scheduler.auth import build_api_client
from betfair_results_downloader.downloader_core import fetch_cleared_orders_df_range
from betfair_results_downloader.csv_utils import update_csv_with_new_data

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("backfill")


def biweekly_chunks(from_date: date, to_date: date) -> list[tuple[date, date]]:
    """Split a date range into ~14-day chunks."""
    chunks: list[tuple[date, date]] = []
    cursor = from_date
    while cursor <= to_date:
        chunk_end = min(cursor + timedelta(days=13), to_date)
        chunks.append((cursor, chunk_end))
        cursor = chunk_end + timedelta(days=1)
    return chunks


def run_one_chunk(
    chunk_from: date,
    chunk_to: date,
    betfair_creds: dict,
    canonical_path: Path,
) -> dict:
    """Download one date range and merge into canonical CSV. Returns result dict."""
    client = None
    try:
        log.info("  Authenticating...")
        client = build_api_client(betfair_creds)

        dl = fetch_cleared_orders_df_range(
            betfair=betfair_creds,
            from_date=chunk_from,
            to_date=chunk_to,
            chunk_days=14,
            api_client=client,
            page_size=200,
            status_cb=lambda msg: log.info("    %s", msg),
        )

        if not dl.attempted or dl.df_co is None or dl.df_co.empty:
            log.info("  No rows returned. %s", dl.message)
            return {"rows": 0, "status": "empty"}

        df = dl.df_co
        rows = len(df)

        # Count enriched fields
        enriched = {}
        for col in ["evt_eventName", "mkt_marketName", "runner_name", "market_type"]:
            if col in df.columns:
                enriched[col] = int(df[col].notna().sum())
            else:
                enriched[col] = 0

        log.info("  Downloaded %d rows", rows)
        log.info("  Enrichment: %s", ", ".join(f"{k}={v}" for k, v in enriched.items()))

        # Merge into canonical CSV
        log.info("  Merging into canonical CSV...")
        update_csv_with_new_data(canonical_path, df)
        log.info("  Merge complete")

        return {"rows": rows, "enriched": enriched, "status": "ok"}

    except Exception as exc:
        log.error("  FAILED: %s: %s", type(exc).__name__, exc)
        log.error("  %s", traceback.format_exc())
        return {"rows": 0, "status": f"error: {exc}", "error": str(exc)}

    finally:
        if client is not None:
            try:
                client.logout()
            except Exception:
                pass


def main():
    parser = argparse.ArgumentParser(description="Backfill with itemDescription enrichment")
    parser.add_argument("--from", dest="from_date", default="2025-12-11")
    parser.add_argument("--to", dest="to_date", default=date.today().isoformat())
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    from_date = date.fromisoformat(args.from_date)
    to_date = date.fromisoformat(args.to_date)

    creds = load_credentials(credentials_path())
    betfair_creds = creds.get("betfair") or {}
    results_dir = resolve_results_dir(creds)
    canonical_path = results_dir / "cleared_orders_cleaned.csv"

    chunks = biweekly_chunks(from_date, to_date)

    log.info("Backfill range: %s -> %s", from_date, to_date)
    log.info("Canonical CSV: %s", canonical_path)
    log.info("Chunks: %d (14-day windows)", len(chunks))

    if args.dry_run:
        for i, (cf, ct) in enumerate(chunks, 1):
            days = (ct - cf).days + 1
            log.info("  Chunk %d/%d: %s -> %s (%d days) [DRY RUN]", i, len(chunks), cf, ct, days)
        return

    total_rows = 0
    ok_count = 0
    fail_count = 0
    empty_count = 0
    start_time = time.time()

    for i, (chunk_from, chunk_to) in enumerate(chunks, 1):
        days = (chunk_to - chunk_from).days + 1
        log.info("")
        log.info("=" * 60)
        log.info("CHUNK %d/%d: %s -> %s (%d days)", i, len(chunks), chunk_from, chunk_to, days)
        log.info("=" * 60)

        chunk_start = time.time()
        result = run_one_chunk(chunk_from, chunk_to, betfair_creds, canonical_path)
        chunk_elapsed = time.time() - chunk_start

        status = result["status"]
        rows = result["rows"]
        total_rows += rows

        if status == "ok":
            ok_count += 1
            log.info("  -> OK: %d rows in %.1fs", rows, chunk_elapsed)
        elif status == "empty":
            empty_count += 1
            log.info("  -> EMPTY in %.1fs", chunk_elapsed)
        else:
            fail_count += 1
            log.info("  -> FAILED in %.1fs: %s", chunk_elapsed, result.get("error", "unknown"))

        # Pause between chunks
        if i < len(chunks):
            log.info("  Pausing 3s...")
            time.sleep(3)

    elapsed = time.time() - start_time
    log.info("")
    log.info("=" * 60)
    log.info("BACKFILL COMPLETE")
    log.info("  Total rows: %d", total_rows)
    log.info("  Chunks: %d ok, %d empty, %d failed (of %d total)", ok_count, empty_count, fail_count, len(chunks))
    log.info("  Elapsed: %.1fs (%.1f min)", elapsed, elapsed / 60)
    log.info("=" * 60)


if __name__ == "__main__":
    main()

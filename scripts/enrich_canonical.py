#!/usr/bin/env python3
"""
enrich_canonical.py
~~~~~~~~~~~~~~~~~~~
Download settled orders with itemDescription and use them to ENRICH
the canonical CSV by updating enrichment columns on matching betIds.

Unlike the backfill script, this does NOT use update_csv_with_new_data
(which at the time had a sort-order bug with mixed types that caused
enriched rows to be discarded during dedup — since fixed in csv_utils by
sorting on typed keys). Instead, it:

1. Downloads data in 14-day chunks (with include_item_description=True)
2. Collects enrichment fields (betId -> runner_name, market_type, etc.)
   into a temporary lookup CSV
3. After all chunks, reads the canonical CSV and fills in the enrichment
   columns via a merge on betId
4. Writes the enriched canonical back atomically

This is safe to run multiple times — it only fills NaN values, never
overwrites existing enrichment.
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
import traceback
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from betfair_results_downloader.secrets import credentials_path, load_credentials
from betfair_results_downloader.paths import resolve_results_dir
from betfair_results_downloader.scheduler.auth import build_api_client
from betfair_results_downloader.downloader_core import fetch_cleared_orders_df_range

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("enrich")

# Columns we want to extract from itemDescription
ENRICH_COLS = [
    "runner_name",
    "market_type",
    "evt_eventName",
    "mkt_marketName",
    "each_way_divisor",
    "evt_countryCode",
]


def biweekly_chunks(from_date: date, to_date: date) -> list[tuple[date, date]]:
    chunks: list[tuple[date, date]] = []
    cursor = from_date
    while cursor <= to_date:
        chunk_end = min(cursor + timedelta(days=13), to_date)
        chunks.append((cursor, chunk_end))
        cursor = chunk_end + timedelta(days=1)
    return chunks


def main():
    parser = argparse.ArgumentParser(description="Enrich canonical CSV with itemDescription data")
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
    enrichment_path = results_dir / ".cache" / "backfill_enrichment.csv"

    chunks = biweekly_chunks(from_date, to_date)

    log.info("Enrichment range: %s -> %s", from_date, to_date)
    log.info("Canonical CSV: %s", canonical_path)
    log.info("Enrichment cache: %s", enrichment_path)
    log.info("Chunks: %d (14-day windows)", len(chunks))

    if args.dry_run:
        for i, (cf, ct) in enumerate(chunks, 1):
            log.info("  Chunk %d/%d: %s -> %s [DRY RUN]", i, len(chunks), cf, ct)
        return

    # ---------------------------------------------------------------
    # Phase 1: Download and collect enrichment data
    # ---------------------------------------------------------------
    log.info("")
    log.info("PHASE 1: Downloading enrichment data from API...")
    log.info("")

    enrichment_path.parent.mkdir(parents=True, exist_ok=True)
    all_enrichment: list[pd.DataFrame] = []
    total_downloaded = 0
    start_time = time.time()

    for i, (chunk_from, chunk_to) in enumerate(chunks, 1):
        days = (chunk_to - chunk_from).days + 1
        log.info("CHUNK %d/%d: %s -> %s (%d days)", i, len(chunks), chunk_from, chunk_to, days)

        client = None
        try:
            client = build_api_client(betfair_creds)
            dl = fetch_cleared_orders_df_range(
                betfair=betfair_creds,
                from_date=chunk_from,
                to_date=chunk_to,
                chunk_days=14,
                api_client=client,
                page_size=200,
            )

            if not dl.attempted or dl.df_co is None or dl.df_co.empty:
                log.info("  No rows. %s", dl.message)
                continue

            df = dl.df_co
            total_downloaded += len(df)

            # Extract only betId + enrichment columns that exist
            available_enrich = [c for c in ENRICH_COLS if c in df.columns]
            keep_cols = ["betId"] + available_enrich
            enrichment_chunk = df[keep_cols].copy()
            # Only keep rows where at least one enrichment col is non-null
            enrich_mask = enrichment_chunk[available_enrich].notna().any(axis=1)
            enrichment_chunk = enrichment_chunk[enrich_mask]

            all_enrichment.append(enrichment_chunk)
            log.info("  Downloaded %d rows, %d with enrichment", len(df), len(enrichment_chunk))

        except Exception as exc:
            log.error("  FAILED: %s: %s", type(exc).__name__, exc)
            log.error("  %s", traceback.format_exc())
        finally:
            if client is not None:
                try:
                    client.logout()
                except Exception:
                    pass

        if i < len(chunks):
            time.sleep(3)

    if not all_enrichment:
        log.error("No enrichment data collected. Exiting.")
        sys.exit(1)

    # Combine all enrichment chunks
    df_enrich = pd.concat(all_enrichment, ignore_index=True)
    df_enrich["betId"] = pd.to_numeric(df_enrich["betId"], errors="coerce")
    df_enrich = df_enrich.drop_duplicates(subset=["betId"], keep="last")

    # Save enrichment cache
    df_enrich.to_csv(enrichment_path, index=False)
    log.info("")
    log.info("Phase 1 complete: %d rows downloaded, %d unique enrichment rows saved",
             total_downloaded, len(df_enrich))
    log.info("Enrichment cache: %s", enrichment_path)

    download_elapsed = time.time() - start_time
    log.info("Download elapsed: %.1fs (%.1f min)", download_elapsed, download_elapsed / 60)

    # ---------------------------------------------------------------
    # Phase 2: Apply enrichment to canonical CSV
    # ---------------------------------------------------------------
    log.info("")
    log.info("PHASE 2: Applying enrichment to canonical CSV...")

    df_canonical = pd.read_csv(canonical_path, low_memory=False)
    log.info("Canonical rows: %d", len(df_canonical))
    df_canonical["betId"] = pd.to_numeric(df_canonical["betId"], errors="coerce")

    # Before stats
    log.info("BEFORE enrichment:")
    for col in ENRICH_COLS:
        if col in df_canonical.columns:
            filled = df_canonical[col].notna().sum()
            log.info("  %s: %d/%d (%.1f%%)", col, filled, len(df_canonical), filled/len(df_canonical)*100)
        else:
            log.info("  %s: COLUMN MISSING (will be created)", col)

    # Merge: left join on betId
    df_enrich_indexed = df_enrich.set_index("betId")

    for col in ENRICH_COLS:
        if col not in df_enrich_indexed.columns:
            continue

        if col not in df_canonical.columns:
            df_canonical[col] = pd.NA

        # Map enrichment values by betId
        enrichment_map = df_enrich_indexed[col].dropna()
        mask = df_canonical["betId"].isin(enrichment_map.index) & df_canonical[col].isna()
        if mask.any():
            df_canonical.loc[mask, col] = df_canonical.loc[mask, "betId"].map(enrichment_map)

    # After stats
    log.info("")
    log.info("AFTER enrichment:")
    for col in ENRICH_COLS:
        if col in df_canonical.columns:
            filled = df_canonical[col].notna().sum()
            log.info("  %s: %d/%d (%.1f%%)", col, filled, len(df_canonical), filled/len(df_canonical)*100)

    # Write back atomically
    log.info("")
    log.info("Writing enriched canonical CSV...")
    tmp_path = canonical_path.with_suffix(".csv.enriching")
    df_canonical.to_csv(tmp_path, index=False)
    tmp_path.replace(canonical_path)
    log.info("Done! Canonical CSV updated.")

    total_elapsed = time.time() - start_time
    log.info("")
    log.info("Total elapsed: %.1fs (%.1f min)", total_elapsed, total_elapsed / 60)


if __name__ == "__main__":
    main()

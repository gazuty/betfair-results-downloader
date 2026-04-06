"""
Google Sheets publisher for market-level Betfair results.

Reads the canonical cleared_orders_cleaned.csv, aggregates to market level,
applies the approval workflow (auto-approve racing, interactive for others),
and uploads approved markets to a designated tab in the existing
"Betfair Dashboard" Google Sheet.

The upload is incremental: new markets are appended, changed markets
(profit updated as more bets settle) are updated in place, and unchanged
markets are left alone.  The sheet is always sorted by SettledDate descending.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import pandas as pd

log = logging.getLogger(__name__)

_MAX_RETRIES = 6
_INITIAL_DELAY = 3.0


# ---------------------------------------------------------------------------
# Result container
# ---------------------------------------------------------------------------


@dataclass
class SheetPublishResult:
    ok: bool
    rows_uploaded: int
    rows_pending: int
    rows_inserted: int = 0
    rows_updated: int = 0
    rows_unchanged: int = 0
    message: str = ""


# ---------------------------------------------------------------------------
# gspread helpers (rate-limit retry)
# ---------------------------------------------------------------------------


def _retry(fn, *args, **kwargs):
    """Retry a gspread call with exponential backoff on 429/RESOURCE_EXHAUSTED."""
    from gspread.exceptions import APIError

    delay = _INITIAL_DELAY
    for attempt in range(_MAX_RETRIES):
        try:
            return fn(*args, **kwargs)
        except APIError as e:
            if "429" in str(e) or "RESOURCE_EXHAUSTED" in str(e):
                log.warning(
                    "Rate limited, retrying in %.0fs (attempt %d/%d)",
                    delay, attempt + 1, _MAX_RETRIES,
                )
                time.sleep(min(delay, 30))
                delay *= 2
            else:
                raise
    raise RuntimeError(f"Gave up after {_MAX_RETRIES} retries")


def _df_to_values(df: pd.DataFrame) -> list[list]:
    """Convert DataFrame to list-of-lists for gspread (header + rows)."""
    rows = [df.columns.tolist()]
    for _, row in df.iterrows():
        cells = []
        for v in row:
            if pd.isna(v):
                cells.append("")
            elif isinstance(v, float):
                cells.append(round(v, 2))
            else:
                cells.append(str(v) if not isinstance(v, (int, str, bool)) else v)
        rows.append(cells)
    return rows


# ---------------------------------------------------------------------------
# Main publish function
# ---------------------------------------------------------------------------

# Columns written to the sheet (marketId included for incremental diffing)
SHEET_COLUMNS = ["marketId", "Sport", "Event", "Market", "SettledDate", "Bets", "Profit"]
SHEET_TAB_NAME = "Market Results"


def _read_existing_sheet(ws) -> pd.DataFrame:
    """Read current sheet contents into a DataFrame keyed by marketId."""
    all_values = _retry(ws.get_all_values)
    if len(all_values) <= 1:
        return pd.DataFrame(columns=SHEET_COLUMNS)
    header = all_values[0]
    df = pd.DataFrame(all_values[1:], columns=header)
    # Coerce types for comparison
    if "Profit" in df.columns:
        df["Profit"] = pd.to_numeric(df["Profit"], errors="coerce")
    if "Bets" in df.columns:
        df["Bets"] = pd.to_numeric(df["Bets"], errors="coerce").astype("Int64")
    if "marketId" in df.columns:
        df["marketId"] = df["marketId"].astype(str)
    return df


def upload_markets_to_sheet(
    df_approved: pd.DataFrame,
    *,
    sheet_name: str,
    service_account_path: Path,
    tab_name: str = SHEET_TAB_NAME,
    dry_run: bool = False,
) -> SheetPublishResult:
    """
    Incrementally sync approved market-level rows to a Google Sheet tab.

    - New markets (marketId not in sheet) are inserted.
    - Changed markets (profit differs) are updated in place.
    - Unchanged markets are left alone.
    - The final sheet is sorted by SettledDate descending.

    If dry_run=True, computes the diff but does not write to the sheet.
    """
    import gspread
    from gspread.exceptions import WorksheetNotFound

    if not service_account_path.exists():
        return SheetPublishResult(
            ok=False, rows_uploaded=0, rows_pending=0,
            message=f"Service account file not found: {service_account_path}",
        )

    # Select and order columns for upload (including marketId for diffing)
    available = [c for c in SHEET_COLUMNS if c in df_approved.columns]
    df_new = df_approved[available].copy()
    df_new["marketId"] = df_new["marketId"].astype(str)
    if "Profit" in df_new.columns:
        df_new["Profit"] = df_new["Profit"].round(2)

    gc = gspread.service_account(filename=str(service_account_path))
    sh = _retry(gc.open, sheet_name)
    log.info("Opened Google Sheet: %s", sheet_name)

    try:
        ws = sh.worksheet(tab_name)
    except WorksheetNotFound:
        ws = _retry(
            sh.add_worksheet,
            tab_name,
            rows=max(len(df_new) + 5, 1000),
            cols=max(len(available) + 2, 10),
        )

    # Read existing sheet data
    df_existing = _read_existing_sheet(ws)

    if df_existing.empty or "marketId" not in df_existing.columns:
        # First run or sheet has no marketId column — full write
        df_final = df_new.sort_values("SettledDate", ascending=False).reset_index(drop=True)
        if not dry_run:
            values = _df_to_values(df_final)
            _retry(ws.clear)
            _retry(ws.update, values, value_input_option="RAW")
        inserted = len(df_final)
        log.info("Initial upload: %d market rows to tab '%s'", inserted, tab_name)
        return SheetPublishResult(
            ok=True,
            rows_uploaded=inserted,
            rows_pending=0,
            rows_inserted=inserted,
            rows_updated=0,
            rows_unchanged=0,
            message=f"Initial upload: {inserted:,} markets to '{tab_name}'.",
        )

    # Incremental diff by marketId
    existing_ids = set(df_existing["marketId"].unique())
    new_ids = set(df_new["marketId"].unique())

    ids_to_insert = new_ids - existing_ids
    ids_in_both = new_ids & existing_ids

    # Check for profit changes in overlapping markets
    ids_to_update = set()
    for mid in ids_in_both:
        old_profit = df_existing.loc[df_existing["marketId"] == mid, "Profit"].iloc[0]
        new_profit = df_new.loc[df_new["marketId"] == mid, "Profit"].iloc[0]
        if pd.isna(old_profit) and pd.isna(new_profit):
            continue
        if pd.isna(old_profit) or pd.isna(new_profit) or round(float(old_profit), 2) != round(float(new_profit), 2):
            ids_to_update.add(mid)

    ids_unchanged = ids_in_both - ids_to_update
    n_inserted = len(ids_to_insert)
    n_updated = len(ids_to_update)
    n_unchanged = len(ids_unchanged)

    log.info(
        "Incremental diff: %d new, %d updated, %d unchanged",
        n_inserted, n_updated, n_unchanged,
    )

    if n_inserted == 0 and n_updated == 0:
        return SheetPublishResult(
            ok=True,
            rows_uploaded=0,
            rows_pending=0,
            rows_inserted=0,
            rows_updated=0,
            rows_unchanged=n_unchanged,
            message=f"Sheet is up to date ({n_unchanged:,} markets unchanged).",
        )

    # Build the merged dataset: existing (with updates applied) + new inserts
    # Start with existing rows, update changed ones
    df_merged = df_existing.copy()

    # Update changed rows
    for mid in ids_to_update:
        new_row = df_new[df_new["marketId"] == mid].iloc[0]
        mask = df_merged["marketId"] == mid
        for col in available:
            if col in df_merged.columns:
                df_merged.loc[mask, col] = new_row[col]

    # Append new rows
    if ids_to_insert:
        df_inserts = df_new[df_new["marketId"].isin(ids_to_insert)]
        df_merged = pd.concat([df_merged, df_inserts], ignore_index=True)

    # Sort by date descending
    if "SettledDate" in df_merged.columns:
        df_merged = df_merged.sort_values("SettledDate", ascending=False).reset_index(drop=True)

    if not dry_run:
        values = _df_to_values(df_merged)
        _retry(ws.clear)
        _retry(ws.update, values, value_input_option="RAW")

    total = len(df_merged)
    action = "Dry run" if dry_run else "Uploaded"
    msg = (
        f"{action}: {total:,} markets to '{tab_name}' "
        f"({n_inserted:,} new, {n_updated:,} updated, {n_unchanged:,} unchanged)."
    )
    log.info(msg)

    return SheetPublishResult(
        ok=True,
        rows_uploaded=total,
        rows_pending=0,
        rows_inserted=n_inserted,
        rows_updated=n_updated,
        rows_unchanged=n_unchanged,
        message=msg,
    )


# ---------------------------------------------------------------------------
# End-to-end orchestration
# ---------------------------------------------------------------------------


def publish_to_sheet(
    *,
    results_csv_dir: Path,
    sheet_name: str,
    service_account_path: Path,
    tab_name: str = SHEET_TAB_NAME,
    interactive: bool = True,
    approval_path: Optional[Path] = None,
    status_cb: Optional[callable] = None,
    dry_run: bool = False,
) -> SheetPublishResult:
    """
    Full pipeline: read canonical CSV → aggregate → approve → upload.

    Parameters
    ----------
    results_csv_dir:
        Directory containing cleared_orders_cleaned.csv
    sheet_name:
        Google Sheet name (e.g. "Betfair Dashboard")
    service_account_path:
        Path to Google service account JSON
    tab_name:
        Worksheet tab name (default "Market Results")
    interactive:
        If True, prompt for approval of non-racing markets.
        If False, only upload previously-approved + auto-approved markets.
    approval_path:
        Path to approved_markets.json. Defaults to <results_csv_dir>/approved_markets.json.
    status_cb:
        Optional progress callback.
    dry_run:
        If True, compute the diff but don't write to the sheet.
    """
    from .market_approval import (
        aggregate_markets,
        split_by_approval,
        interactive_approve,
    )

    def say(msg: str) -> None:
        log.info(msg)
        if status_cb:
            try:
                status_cb(msg)
            except Exception:
                pass

    canonical_path = results_csv_dir / "cleared_orders_cleaned.csv"
    if not canonical_path.exists():
        return SheetPublishResult(
            ok=False, rows_uploaded=0, rows_pending=0,
            message=f"Canonical CSV not found: {canonical_path}",
        )

    if approval_path is None:
        approval_path = results_csv_dir / "approved_markets.json"

    # Step 1: Read canonical CSV
    say("Reading canonical CSV...")
    df_canonical = pd.read_csv(canonical_path, low_memory=False)
    say(f"Loaded {len(df_canonical):,} bet rows from canonical CSV.")

    # Step 2: Aggregate to market level
    say("Aggregating to market level...")
    df_markets = aggregate_markets(df_canonical)
    say(f"Aggregated to {len(df_markets):,} markets.")

    # Step 3: Split into approved / pending
    df_approved, df_pending = split_by_approval(df_markets, approval_path)
    say(
        f"Auto/previously approved: {len(df_approved):,} markets, "
        f"pending review: {len(df_pending):,} markets."
    )

    # Step 4: Interactive approval of pending markets (if enabled)
    newly_approved_count = 0
    if interactive and not df_pending.empty:
        df_newly_approved = interactive_approve(df_pending, approval_path)
        newly_approved_count = len(df_newly_approved)
        if not df_newly_approved.empty:
            df_approved = pd.concat([df_approved, df_newly_approved], ignore_index=True)
            say(f"Total approved after review: {len(df_approved):,} markets.")

    still_pending = len(df_pending) - newly_approved_count

    if df_approved.empty:
        return SheetPublishResult(
            ok=True, rows_uploaded=0, rows_pending=still_pending,
            message="No approved markets to upload.",
        )

    # Step 5: Upload to Google Sheets (incremental)
    say("Syncing to Google Sheets...")
    result = upload_markets_to_sheet(
        df_approved,
        sheet_name=sheet_name,
        service_account_path=service_account_path,
        tab_name=tab_name,
        dry_run=dry_run,
    )
    result.rows_pending = still_pending
    return result

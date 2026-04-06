"""
Market approval workflow for Google Sheets publishing.

Horse Racing and Greyhound Racing markets are auto-approved (they settle quickly).
All other sports (tennis, cricket, season-long markets, etc.) require explicit
user approval before uploading, because they may take days/weeks/months to
fully settle and uploading early shows a false result.

Approved market IDs are persisted in a JSON file so approvals survive across runs.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import pandas as pd

from .config import EVENTTYPE_HORSES, EVENTTYPE_GREYHOUNDS

log = logging.getLogger(__name__)

EVENTTYPE_SOCCER = 1

# Sports that settle quickly and can be uploaded automatically.
# Everything else (golf, motor racing, AFL premiership, tennis tournaments,
# cricket series, etc.) requires manual approval because markets may take
# days/weeks/months to fully settle.
AUTO_APPROVE_EVENT_TYPES: set[int] = {
    EVENTTYPE_HORSES,
    EVENTTYPE_GREYHOUNDS,
    EVENTTYPE_SOCCER,
}

# ---------------------------------------------------------------------------
# Approval file I/O
# ---------------------------------------------------------------------------


def _default_approval_path(results_csv_dir: Path) -> Path:
    return results_csv_dir / "approved_markets.json"


def load_approvals(path: Path) -> dict[str, str]:
    """
    Load the approved-markets mapping.

    Returns {marketId: iso_timestamp_of_approval}.
    """
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        log.warning("Could not parse approval file %s — starting fresh", path)
        return {}


def save_approvals(approvals: dict[str, str], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(approvals, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Market-level aggregation
# ---------------------------------------------------------------------------

_SPORT_LABELS: dict[int, str] = {
    EVENTTYPE_HORSES: "Horse Racing",
    EVENTTYPE_GREYHOUNDS: "Greyhound Racing",
    EVENTTYPE_SOCCER: "Soccer",
}


def aggregate_markets(df_canonical: pd.DataFrame) -> pd.DataFrame:
    """
    Aggregate per-bet rows into market-level rows.

    Input: the canonical cleared_orders_cleaned.csv DataFrame.
    Output: one row per marketId with columns:
        marketId, eventTypeId, Sport, Event, Market, SettledDate, Bets, Profit
    """
    required = {"marketId", "eventTypeId", "profit", "betId", "settledDate"}
    missing = required - set(df_canonical.columns)
    if missing:
        raise ValueError(f"Canonical CSV is missing columns: {sorted(missing)}")

    df = df_canonical.copy()
    df["eventTypeId"] = pd.to_numeric(df["eventTypeId"], errors="coerce").astype("Int64")
    df["profit"] = pd.to_numeric(df["profit"], errors="coerce")
    df["settledDate"] = pd.to_datetime(df["settledDate"], utc=True, errors="coerce")

    # Build event/market description from itemDescription fields if available
    event_col = _pick_col(df, ["itemDescription.eventDesc", "evt_eventName"])
    market_col = _pick_col(df, ["itemDescription.marketDesc", "mkt_marketName"])

    agg_dict: dict[str, tuple] = {
        "Profit": ("profit", "sum"),
        "Bets": ("betId", "count"),
        "SettledDate": ("settledDate", "max"),
        "eventTypeId": ("eventTypeId", "first"),
    }

    if event_col:
        df["_event"] = df[event_col].fillna("")
        agg_dict["Event"] = ("_event", "first")

    if market_col:
        df["_market"] = df[market_col].fillna("")
        agg_dict["Market"] = ("_market", "first")

    df_mkt = df.groupby("marketId", as_index=False).agg(**agg_dict)

    # Map eventTypeId to sport label
    df_mkt["Sport"] = df_mkt["eventTypeId"].map(_SPORT_LABELS).fillna("Other")

    # Fill missing event/market columns
    if "Event" not in df_mkt.columns:
        df_mkt["Event"] = ""
    if "Market" not in df_mkt.columns:
        df_mkt["Market"] = ""

    # Format settled date as date string
    df_mkt["SettledDate"] = df_mkt["SettledDate"].dt.strftime("%Y-%m-%d")

    # Round profit
    df_mkt["Profit"] = df_mkt["Profit"].round(2)

    return df_mkt[["marketId", "eventTypeId", "Sport", "Event", "Market",
                    "SettledDate", "Bets", "Profit"]]


def _pick_col(df: pd.DataFrame, candidates: list[str]) -> Optional[str]:
    """Return the first column name from candidates that exists in df."""
    for c in candidates:
        if c in df.columns:
            return c
    return None


# ---------------------------------------------------------------------------
# Approval logic
# ---------------------------------------------------------------------------


def split_by_approval(
    df_markets: pd.DataFrame,
    approval_path: Path,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    Split market-level DataFrame into (approved, pending).

    - Racing markets (Horse Racing / Greyhound Racing) are always approved.
    - Other markets are approved only if their marketId is in the approvals file.
    """
    approvals = load_approvals(approval_path)

    is_racing = df_markets["eventTypeId"].isin(AUTO_APPROVE_EVENT_TYPES)
    is_previously_approved = df_markets["marketId"].astype(str).isin(approvals.keys())

    approved_mask = is_racing | is_previously_approved
    df_approved = df_markets[approved_mask].copy()
    df_pending = df_markets[~approved_mask].copy()

    return df_approved, df_pending


def interactive_approve(
    df_pending: pd.DataFrame,
    approval_path: Path,
) -> pd.DataFrame:
    """
    Present pending (non-racing) markets to the user interactively.

    Returns the newly-approved subset. Approved market IDs are persisted
    to the approvals file.
    """
    if df_pending.empty:
        print("No pending markets to review.")
        return df_pending.iloc[0:0]  # empty with same schema

    approvals = load_approvals(approval_path)
    newly_approved: list[str] = []

    print()
    print("=" * 70)
    print("PENDING MARKETS — these need your approval before uploading")
    print("(Non-racing markets may not be fully settled yet)")
    print("=" * 70)
    print()

    # Show summary table
    display_cols = ["Sport", "Event", "Market", "SettledDate", "Bets", "Profit"]
    available = [c for c in display_cols if c in df_pending.columns]

    for idx, (_, row) in enumerate(df_pending.iterrows(), start=1):
        parts = [f"  {idx}. "]
        for col in available:
            parts.append(f"{col}: {row[col]}")
        print(" | ".join(parts))

    print()
    print("Options:")
    print("  a = approve ALL pending markets")
    print("  n = approve NONE (skip all)")
    print("  Or enter numbers separated by commas (e.g. 1,3,5)")
    print()

    choice = input("Your choice: ").strip().lower()

    if choice == "a":
        selected_indices = list(range(len(df_pending)))
    elif choice == "n" or choice == "":
        selected_indices = []
    else:
        try:
            selected_indices = [int(x.strip()) - 1 for x in choice.split(",")]
            selected_indices = [i for i in selected_indices if 0 <= i < len(df_pending)]
        except ValueError:
            print("Invalid input — skipping all.")
            selected_indices = []

    now = datetime.now(timezone.utc).isoformat()
    rows = df_pending.iloc[selected_indices] if selected_indices else df_pending.iloc[0:0]

    for _, row in rows.iterrows():
        mid = str(row["marketId"])
        approvals[mid] = now
        newly_approved.append(mid)

    if newly_approved:
        save_approvals(approvals, approval_path)
        print(f"\nApproved {len(newly_approved)} market(s).")
    else:
        print("\nNo markets approved.")

    return rows

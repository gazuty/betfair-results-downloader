from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable, Optional

import pandas as pd

logger = logging.getLogger(__name__)


def clean_and_remove_duplicates(
    df: pd.DataFrame,
    *,
    status_cb: Optional[Callable[[str], None]] = None,
) -> pd.DataFrame:
    """
    Minimal, robust dedupe for cleared orders-like data.

    Primary key: betId (best identifier for cleared orders).
    Falls back to full row dedupe if betId missing.

    Args:
        df: DataFrame to deduplicate
        status_cb: Optional callback for status messages (warnings visible in GUI)
    """

    def warn(msg: str) -> None:
        """Log and emit warning via status callback."""
        logger.warning(msg)
        if status_cb:
            try:
                status_cb(msg)
            except Exception:
                pass

    if df is None or df.empty:
        return df.copy() if df is not None else pd.DataFrame()

    out = df.copy()
    total_rows = len(out)

    if "betId" in out.columns:
        # coerce to numeric when possible (handles string/float-like)
        out["betId"] = pd.to_numeric(out["betId"], errors="coerce")

        nan_count = int(out["betId"].isna().sum())
        valid_count = total_rows - nan_count

        if nan_count > 0 and valid_count > 0:
            # Some betIds are invalid, but we can still dedupe on valid ones
            warn(
                f"DEDUPE WARNING: {nan_count:,} of {total_rows:,} rows have invalid betId values"
            )

        if out["betId"].notna().any():
            # stable ordering improves reproducibility
            sort_cols = [
                c
                for c in ["settledDate", "placedDate", "marketId", "betId"]
                if c in out.columns
            ]
            if sort_cols:
                out = out.sort_values(sort_cols, kind="mergesort")

            out = out.drop_duplicates(subset=["betId"], keep="last").reset_index(
                drop=True
            )
            return out
        else:
            # All betIds are NaN - must fall back
            warn(
                f"DEDUPE WARNING: All {total_rows:,} betId values invalid — falling back to full-row dedupe"
            )

    else:
        # betId column missing entirely
        warn("DEDUPE WARNING: betId column missing — using full-row dedupe")

    # fallback: full-row dedupe
    return out.drop_duplicates(keep="last").reset_index(drop=True)


def update_csv_with_new_data(
    existing_csv_path: str | Path,
    new_data_df: pd.DataFrame,
    *,
    status_cb: Optional[Callable[[str], None]] = None,
) -> Path:
    """
    Idempotently update a canonical CSV with new data:
    - create parent directory if missing
    - read existing if present
    - union columns
    - concat + dedupe
    - atomic write (tmp then replace)

    Args:
        existing_csv_path: Path to canonical CSV
        new_data_df: New data to add
        status_cb: Optional callback for status messages (warnings visible in GUI)
    """
    path = Path(existing_csv_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    incoming = clean_and_remove_duplicates(new_data_df, status_cb=status_cb)

    if path.exists():
        existing = pd.read_csv(path)

        # union schema and align
        cols = sorted(set(existing.columns).union(set(incoming.columns)))
        existing = existing.reindex(columns=cols)
        incoming = incoming.reindex(columns=cols)

        combined = pd.concat([existing, incoming], ignore_index=True)
    else:
        combined = incoming

    combined = clean_and_remove_duplicates(combined, status_cb=status_cb)

    tmp_path = path.with_suffix(path.suffix + ".tmp")
    combined.to_csv(tmp_path, index=False)
    tmp_path.replace(path)

    return path

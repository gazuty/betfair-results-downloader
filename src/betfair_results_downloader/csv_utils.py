from __future__ import annotations

from pathlib import Path
import pandas as pd


def clean_and_remove_duplicates(df: pd.DataFrame) -> pd.DataFrame:
    """
    Minimal, robust dedupe for cleared orders-like data.

    Primary key: betId (best identifier for cleared orders).
    Falls back to full row dedupe if betId missing.
    """
    if df is None or df.empty:
        return df.copy() if df is not None else pd.DataFrame()

    out = df.copy()

    if "betId" in out.columns:
        # coerce to numeric when possible (handles string/float-like)
        out["betId"] = pd.to_numeric(out["betId"], errors="coerce")

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

    # fallback: full-row dedupe
    return out.drop_duplicates(keep="last").reset_index(drop=True)


def update_csv_with_new_data(
    existing_csv_path: str | Path, new_data_df: pd.DataFrame
) -> Path:
    """
    Idempotently update a canonical CSV with new data:
    - create parent directory if missing
    - read existing if present
    - union columns
    - concat + dedupe
    - atomic write (tmp then replace)
    """
    path = Path(existing_csv_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    incoming = clean_and_remove_duplicates(new_data_df)

    if path.exists():
        existing = pd.read_csv(path)

        # union schema and align
        cols = sorted(set(existing.columns).union(set(incoming.columns)))
        existing = existing.reindex(columns=cols)
        incoming = incoming.reindex(columns=cols)

        combined = pd.concat([existing, incoming], ignore_index=True)
    else:
        combined = incoming

    combined = clean_and_remove_duplicates(combined)

    tmp_path = path.with_suffix(path.suffix + ".tmp")
    combined.to_csv(tmp_path, index=False)
    tmp_path.replace(path)

    return path

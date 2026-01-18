from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path
from typing import Optional, Tuple

import pandas as pd


FALLBACK_DAYS = 90
FALLBACK_NOTE = "No existing data - Recommend 90 days capture, however this may take some time."


def recommend_lookback_days(
    results_csv_dir: Path,
    canonical_filename: str = "cleared_orders_cleaned.csv",
) -> Tuple[int, str, Optional[date]]:
    canonical_path = results_csv_dir / canonical_filename
    if not canonical_path.exists():
        return FALLBACK_DAYS, FALLBACK_NOTE, None

    try:
        df = pd.read_csv(canonical_path, usecols=["settledDate"], low_memory=False)
    except Exception:
        return FALLBACK_DAYS, FALLBACK_NOTE, None

    if "settledDate" not in df.columns or df.empty:
        return FALLBACK_DAYS, FALLBACK_NOTE, None

    ts = pd.to_datetime(df["settledDate"], utc=True, errors="coerce").dropna()
    if ts.empty:
        return FALLBACK_DAYS, FALLBACK_NOTE, None

    last_settled_utc = ts.max().date()
    today_utc = datetime.now(timezone.utc).date()
    gap_days = max((today_utc - last_settled_utc).days, 0)
    recommended_days = min(90, max(1, gap_days + 1))

    note = (
        f"Existing data found (latest settledDate: {last_settled_utc:%Y-%m-%d} UTC) - "
        f"Recommend capture {recommended_days} days (includes 1-day overlap)."
    )
    return recommended_days, note, last_settled_utc

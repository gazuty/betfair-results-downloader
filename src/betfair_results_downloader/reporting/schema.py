from __future__ import annotations

from zoneinfo import ZoneInfo
import pandas as pd


SYDNEY_TZ = ZoneInfo("Australia/Sydney")
HORSES_LABEL = "Horses"
GREYHOUNDS_LABEL = "Greyhounds"

# Betfair event type IDs (extend later if needed)
EVENT_TYPE_MAP = {
    7: HORSES_LABEL,
    4339: GREYHOUNDS_LABEL,
}


def _to_utc_datetime(series: pd.Series) -> pd.Series:
    """
    Parse timestamps that are expected to be UTC.
    Returns tz-aware UTC datetimes (or NaT).
    """
    return pd.to_datetime(series, errors="coerce", utc=True)


def normalize_cleared_orders_schema(df: pd.DataFrame) -> pd.DataFrame:
    """
    Normalize core columns and add derived time columns:
      - placed_dt_utc, settled_dt_utc (tz-aware)
      - placed_dt_local, settled_dt_local (Australia/Sydney)
      - settled_date_local (date)
    Also ensures profit is numeric and adds outcome helpers.
    Adds 'sport' derived from eventTypeId.
    """
    out = df.copy()

    # Time columns (source is UTC)
    if "placedDate" in out.columns:
        out["placed_dt_utc"] = _to_utc_datetime(out["placedDate"])
        out["placed_dt_local"] = out["placed_dt_utc"].dt.tz_convert(SYDNEY_TZ)
    else:
        out["placed_dt_utc"] = pd.NaT
        out["placed_dt_local"] = pd.NaT

    if "settledDate" in out.columns:
        out["settled_dt_utc"] = _to_utc_datetime(out["settledDate"])
        out["settled_dt_local"] = out["settled_dt_utc"].dt.tz_convert(SYDNEY_TZ)
    else:
        out["settled_dt_utc"] = pd.NaT
        out["settled_dt_local"] = pd.NaT

    # Profit
    if "profit" in out.columns:
        out["profit"] = pd.to_numeric(out["profit"], errors="coerce").fillna(0.0)
    else:
        out["profit"] = 0.0

    # Outcome helpers
    if "betOutcome" in out.columns:
        bo = out["betOutcome"].astype(str).str.upper()
        out["is_win"] = bo.eq("WON")
        out["is_loss"] = bo.eq("LOST")
    else:
        out["is_win"] = out["profit"] > 0
        out["is_loss"] = out["profit"] < 0

    # Convenience local dates
    out["settled_date_local"] = out["settled_dt_local"].dt.date

    # Human-readable sport label from eventTypeId
    if "eventTypeId" in out.columns:
        et = pd.to_numeric(out["eventTypeId"], errors="coerce")
        out["sport"] = et.map(EVENT_TYPE_MAP)
        out["sport"] = out["sport"].fillna(
            et.apply(lambda x: f"Other ({int(x)})" if pd.notna(x) else "Unknown")
        )
    else:
        out["sport"] = "Unknown"

    return out

from __future__ import annotations

from zoneinfo import ZoneInfo
import pandas as pd


SYDNEY_TZ = ZoneInfo("Australia/Sydney")
HORSES_LABEL = "Horses"
GREYHOUNDS_LABEL = "Greyhounds"

# Betfair event type IDs, per the public listEventTypes catalogue.
EVENT_TYPE_MAP = {
    1: "Soccer",
    2: "Tennis",
    3: "Golf",
    4: "Cricket",
    5: "Rugby Union",
    6: "Boxing",
    7: HORSES_LABEL,
    8: "Motor Sport",
    10: "Special Bets",
    11: "Cycling",
    1477: "Rugby League",
    3503: "Darts",
    3988: "Athletics",
    4339: GREYHOUNDS_LABEL,
    6231: "Financial Bets",
    6422: "Snooker",
    6423: "American Football",
    7511: "Baseball",
    7522: "Basketball",
    7524: "Ice Hockey",
    61420: "Australian Rules",
    468328: "Handball",
    998916: "Yachting",
    998917: "Volleyball",
    2152880: "Gaelic Games",
    2378961: "Politics",
    26420387: "Mixed Martial Arts",
    27454571: "Esports",
}


def sport_label(event_type_id: object) -> str:
    """
    Map a single eventTypeId to its human-readable sport label.
    Unknown numeric ids fall back to "Other (<id>)"; anything that
    doesn't parse as a number (including None/NaN) is "Unknown".
    """
    try:
        if event_type_id is None or pd.isna(event_type_id):
            return "Unknown"
        numeric_id = int(float(event_type_id))
    except (TypeError, ValueError):
        return "Unknown"
    return EVENT_TYPE_MAP.get(numeric_id, f"Other ({numeric_id})")


def _to_utc_datetime(series: pd.Series) -> pd.Series:
    """
    Parse timestamps that are expected to be UTC.
    Returns tz-aware UTC datetimes (or NaT).
    """
    return pd.to_datetime(series, errors="coerce", utc=True, format="ISO8601")


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
        out["sport"] = out["eventTypeId"].apply(sport_label)
    else:
        out["sport"] = "Unknown"

    return out

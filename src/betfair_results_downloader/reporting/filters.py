from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import streamlit as st
import pandas as pd


@dataclass(frozen=True)
class FilterState:
    date_basis: str
    date_from: date | None
    date_to: date | None
    sports: list[str]
    countries: list[str]
    tracks: list[str]


def _safe_unique_sorted(df: pd.DataFrame, col: str) -> list[str]:
    if col not in df.columns:
        return []
    vals = df[col].dropna().astype(str)
    vals = [
        v for v in vals.unique().tolist() if v.strip() and v.strip().lower() != "nan"
    ]
    return sorted(vals)


def apply_sidebar_filters(df: pd.DataFrame) -> tuple[pd.DataFrame, FilterState]:
    """
    Applies sidebar filters. All filters are optional and degrade gracefully if
    columns are missing.

    Date filtering uses local timestamps (Australia/Sydney).
    Sport filtering uses derived column 'sport' from schema.py.
    Track filter currently uses mkt_marketName (we can swap to evt_venue next).
    """
    with st.sidebar:
        st.header("Filters")

        date_basis = st.selectbox(
            "Date basis",
            ["Settled (recommended)", "Placed"],
            index=0,
            help="Most reporting uses settled time; placed is useful for activity analysis.",
        )

        dt_col = (
            "settled_dt_local"
            if date_basis.startswith("Settled")
            else "placed_dt_local"
        )

        # Determine available min/max for date picker
        if dt_col in df.columns and df[dt_col].notna().any():
            min_d = df[dt_col].min().date()
            max_d = df[dt_col].max().date()
        else:
            min_d, max_d = None, None

        date_range = st.date_input(
            "Date range (local)",
            value=(min_d, max_d) if (min_d and max_d) else (),
        )

        date_from = None
        date_to = None
        if isinstance(date_range, tuple) and len(date_range) == 2:
            date_from, date_to = date_range

        # Filter values (optional based on columns)
        sport_values = _safe_unique_sorted(df, "sport")
        country_values = _safe_unique_sorted(df, "evt_countryCode")
        track_values = _safe_unique_sorted(df, "mkt_marketName")

        sports = st.multiselect(
            "Sport",
            options=sport_values,
            default=[s for s in ["Horses", "Greyhounds"] if s in sport_values],
            help="Filters by derived sport label (from eventTypeId).",
        )

        countries = st.multiselect(
            "Country (evt_countryCode)",
            options=country_values,
            default=[],
            help="Uses enrichment column evt_countryCode if present.",
        )

        tracks = st.multiselect(
            "Track / Market (mkt_marketName)",
            options=track_values,
            default=[],
            help="Currently uses mkt_marketName. Next improvement: use evt_venue for true track/venue.",
        )

    out = df.copy()

    # Date filter
    if date_from and date_to and dt_col in out.columns:
        out = out.loc[out[dt_col].dt.date.between(date_from, date_to)]

    # Sport filter
    if sports and "sport" in out.columns:
        out = out.loc[out["sport"].astype(str).isin(sports)]

    # Country filter
    if countries and "evt_countryCode" in out.columns:
        out = out.loc[out["evt_countryCode"].astype(str).isin(countries)]

    # Track/Market filter
    if tracks and "mkt_marketName" in out.columns:
        out = out.loc[out["mkt_marketName"].astype(str).isin(tracks)]

    state = FilterState(
        date_basis=date_basis,
        date_from=date_from,
        date_to=date_to,
        sports=sports,
        countries=countries,
        tracks=tracks,
    )
    return out, state

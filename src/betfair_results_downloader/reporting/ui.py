from __future__ import annotations

from dataclasses import asdict
from typing import Any, Optional

import pandas as pd
import streamlit as st


def _fmt_currency(x: float) -> str:
    try:
        return f"{x:,.2f}"
    except Exception:
        return str(x)


def _fmt_percent(x: float) -> str:
    try:
        return f"{x * 100:,.1f}%"
    except Exception:
        return str(x)


def human_bytes(n: int) -> str:
    if n is None:
        return ""
    step = 1024.0
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if n < step:
            return f"{n:,.0f} {unit}" if unit == "B" else f"{n:,.2f} {unit}"
        n /= step
    return f"{n:,.2f} PB"


def render_app_header() -> None:
    st.title("Betfair Reporting Dashboard")
    st.caption("Local CSV reporting • Australia/Sydney • Weeks: Sunday–Saturday")


def render_dataset_header(
    *,
    file_meta: dict,
    rows: int,
    date_min: Optional[pd.Timestamp],
    date_max: Optional[pd.Timestamp],
) -> None:
    """
    Clean “data loaded” header.
    """
    cols = st.columns([2, 1, 1, 1])
    with cols[0]:
        name = file_meta.get("name", "Unknown file")
        st.subheader(name)

        sub = []
        if file_meta.get("dir"):
            sub.append(file_meta["dir"])
        if file_meta.get("modified"):
            sub.append(f"Modified: {file_meta['modified']:%Y-%m-%d %H:%M}")
        if file_meta.get("size_bytes") is not None:
            sub.append(f"Size: {human_bytes(int(file_meta['size_bytes']))}")
        st.caption(" • ".join(sub))

    with cols[1]:
        st.metric("Rows", f"{rows:,}")
    with cols[2]:
        if date_min is not None:
            st.metric("From", f"{date_min:%Y-%m-%d}")
        else:
            st.metric("From", "—")
    with cols[3]:
        if date_max is not None:
            st.metric("To", f"{date_max:%Y-%m-%d}")
        else:
            st.metric("To", "—")


def render_filter_summary(filter_state: Any) -> None:
    """
    Compact summary of active filters (expects your FilterState dataclass).
    """
    d = asdict(filter_state)

    bits: list[str] = []
    bits.append(f"Date basis: {d.get('date_basis', '—')}")
    if d.get("date_from") and d.get("date_to"):
        bits.append(f"Range: {d['date_from']} → {d['date_to']}")

    sports = d.get("sports") or d.get("event_types") or []
    if sports:
        bits.append(f"Sport: {', '.join(sports)}")

    countries = d.get("countries") or []
    if countries:
        bits.append(f"Country: {', '.join(countries)}")

    tracks = d.get("tracks") or []
    if tracks:
        # Avoid enormous summaries
        if len(tracks) > 5:
            bits.append(f"Track/Market: {tracks[0]} +{len(tracks) - 1} more")
        else:
            bits.append(f"Track/Market: {', '.join(tracks)}")

    st.info(" • ".join(bits))


def kpi_row(*, bets: int, profit: float, avg_profit: float, strike_rate: float) -> None:
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Bets", f"{bets:,}")
    c2.metric("Profit", _fmt_currency(profit))
    c3.metric("Avg / Bet", _fmt_currency(avg_profit))
    c4.metric("Strike Rate", _fmt_percent(strike_rate))


def dataframe_pretty(
    df: pd.DataFrame, *, profit_cols: Optional[list[str]] = None
) -> None:
    """
    Consistent formatting for tables across the app.
    """
    profit_cols = profit_cols or []
    col_config: dict[str, Any] = {}

    for c in df.columns:
        if c in profit_cols or c.lower() in {"profit", "avg_profit"}:
            col_config[c] = st.column_config.NumberColumn(format="%.2f")
        if c.lower() == "strike_rate":
            col_config[c] = st.column_config.NumberColumn(format="%.3f")
        if (
            "date" in c.lower()
            or c.lower().endswith("_start")
            or c.lower().endswith("_end")
            or c.lower() in {"day", "week_start", "week_end"}
        ):
            # Streamlit will handle python date objects well; keep it simple
            pass

    st.dataframe(
        df, use_container_width=True, hide_index=True, column_config=col_config
    )

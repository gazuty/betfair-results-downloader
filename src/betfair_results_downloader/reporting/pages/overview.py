from __future__ import annotations

import streamlit as st
import plotly.express as px
import pandas as pd

from betfair_results_downloader.reporting.transforms import (
    daily_agg,
    weekly_agg_sun_start,
)
from betfair_results_downloader.reporting.exports import download_df_as_csv
from betfair_results_downloader.reporting.ui import kpi_row, dataframe_pretty


def render_overview(df: pd.DataFrame, filt_state) -> None:
    st.subheader("Overview")

    bets = len(df)
    profit = float(df["profit"].sum()) if "profit" in df.columns else 0.0
    avg_profit = float(df["profit"].mean()) if bets else 0.0
    strike = float(df["is_win"].mean()) if bets and "is_win" in df.columns else 0.0
    kpi_row(bets=bets, profit=profit, avg_profit=avg_profit, strike_rate=strike)

    st.divider()

    d = daily_agg(df, dt_col="settled_dt_local")
    if d.empty:
        st.info("No data after filters.")
        return

    st.write("### Daily Profit (local)")
    fig = px.line(d, x="day", y="profit")
    st.plotly_chart(fig, use_container_width=True)

    with st.expander("Daily table", expanded=False):
        dataframe_pretty(d, profit_cols=["profit", "avg_profit"])
        download_df_as_csv(d, "daily_profit.csv")

    st.write("### Weekly Profit (Sunday–Saturday)")
    w = weekly_agg_sun_start(df, dt_col="settled_dt_local")
    figw = px.bar(w, x="week_start", y="profit")
    st.plotly_chart(figw, use_container_width=True)

    with st.expander("Weekly table", expanded=False):
        dataframe_pretty(w, profit_cols=["profit", "avg_profit"])
        download_df_as_csv(w, "weekly_profit_sun_start.csv")

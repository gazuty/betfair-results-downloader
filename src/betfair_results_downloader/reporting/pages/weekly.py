from __future__ import annotations

import streamlit as st
import plotly.express as px
import pandas as pd

from betfair_results_downloader.reporting.transforms import weekly_agg_sun_start
from betfair_results_downloader.reporting.exports import download_df_as_csv
from betfair_results_downloader.reporting.ui import dataframe_pretty


def render_weekly(df: pd.DataFrame, filt_state) -> None:
    st.subheader("Weekly (Sunday–Saturday)")

    w = weekly_agg_sun_start(df, dt_col="settled_dt_local")
    if w.empty:
        st.info("No data after filters.")
        return

    fig = px.bar(w, x="week_start", y="profit")
    st.plotly_chart(fig, use_container_width=True)

    dataframe_pretty(w, profit_cols=["profit", "avg_profit"])
    download_df_as_csv(w, "weekly_sun_start.csv")

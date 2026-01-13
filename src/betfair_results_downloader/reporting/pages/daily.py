from __future__ import annotations

import streamlit as st
import plotly.express as px
import pandas as pd

from betfair_results_downloader.reporting.transforms import daily_agg
from betfair_results_downloader.reporting.exports import download_df_as_csv
from betfair_results_downloader.reporting.ui import dataframe_pretty


def render_daily(df: pd.DataFrame, filt_state) -> None:
    st.subheader("Daily")

    d = daily_agg(df, dt_col="settled_dt_local")
    if d.empty:
        st.info("No data after filters.")
        return

    fig = px.line(d, x="day", y="profit")
    st.plotly_chart(fig, use_container_width=True)

    dataframe_pretty(d, profit_cols=["profit", "avg_profit"])
    download_df_as_csv(d, "daily.csv")

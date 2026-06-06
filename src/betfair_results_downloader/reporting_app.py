from __future__ import annotations

import streamlit as st

from betfair_results_downloader.reporting.io import (
    build_cached_csv_loader,
    discover_csv_files,
    file_info,
)
from betfair_results_downloader.reporting.schema import normalize_cleared_orders_schema
from betfair_results_downloader.reporting.filters import apply_sidebar_filters

from betfair_results_downloader.reporting.ui import (
    render_app_header,
    render_dataset_header,
    render_filter_summary,
)

from betfair_results_downloader.reporting.pages.overview import render_overview
from betfair_results_downloader.reporting.pages.daily import render_daily
from betfair_results_downloader.reporting.pages.weekly import render_weekly


st.set_page_config(
    page_title="Betfair Reporting Dashboard",
    layout="wide",
    initial_sidebar_state="expanded",
)

load_csv = build_cached_csv_loader(st.cache_data)

render_app_header()

# -------- Sidebar: data source + nav --------
DEFAULT_RESULTS_DIR = (
    r"C:\Users\Mark\OneDrive\BF Documentation\BF Results and Analysis\Results Database"
)

if "results_dir" not in st.session_state:
    st.session_state["results_dir"] = DEFAULT_RESULTS_DIR

with st.sidebar:
    st.header("Data")
    col_a, col_b = st.columns([1, 1])
    with col_a:
        if st.button("Use default folder", use_container_width=True):
            st.session_state["results_dir"] = DEFAULT_RESULTS_DIR
    with col_b:
        if st.button("Clear", use_container_width=True):
            st.session_state["results_dir"] = ""

    results_dir = st.text_input(
        "CSV folder",
        value=st.session_state["results_dir"],
        placeholder=r"C:\Path\To\Results Database",
        help="Folder containing cleared orders CSVs (canonical + snapshots).",
    )
    st.session_state["results_dir"] = results_dir

    discovered = discover_csv_files(results_dir) if results_dir else []
    if not discovered:
        st.warning("No cleared orders CSV files found in that folder.")
        st.stop()

    options = {f"{p.name}": str(p) for p in discovered}
    selected_name = st.selectbox("Select file", list(options.keys()))
    selected_path = options[selected_name]

    st.divider()
    st.header("Navigate")
    page = st.radio("Page", ["Overview", "Daily", "Weekly"], index=0)

# -------- Load + normalize --------
meta = file_info(selected_path)
df_raw = load_csv(selected_path)
df = normalize_cleared_orders_schema(df_raw)

# Determine date range for header (use settled local as primary)
date_min = (
    df["settled_dt_local"].min()
    if "settled_dt_local" in df.columns and df["settled_dt_local"].notna().any()
    else None
)
date_max = (
    df["settled_dt_local"].max()
    if "settled_dt_local" in df.columns and df["settled_dt_local"].notna().any()
    else None
)

render_dataset_header(
    file_meta=meta,
    rows=len(df),
    date_min=date_min,
    date_max=date_max,
)

# -------- Filters --------
df_filt, filt_state = apply_sidebar_filters(df)
render_filter_summary(filt_state)

st.divider()

# -------- Render page --------
if page == "Overview":
    render_overview(df_filt, filt_state)
elif page == "Daily":
    render_daily(df_filt, filt_state)
elif page == "Weekly":
    render_weekly(df_filt, filt_state)
else:
    st.error("Unknown page.")

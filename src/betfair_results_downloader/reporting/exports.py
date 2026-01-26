from __future__ import annotations

import pandas as pd
import streamlit as st


def download_df_as_csv(
    df: pd.DataFrame, filename: str, label: str = "Download CSV"
) -> None:
    csv_bytes = df.to_csv(index=False).encode("utf-8")
    st.download_button(
        label=label,
        data=csv_bytes,
        file_name=filename,
        mime="text/csv",
    )

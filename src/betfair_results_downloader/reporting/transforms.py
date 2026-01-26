from __future__ import annotations

import pandas as pd


def _week_start_sunday(d: pd.Series) -> pd.Series:
    """
    Given a Series of datetime (tz-aware), return week start (Sunday) as date.
    Week definition: Sunday–Saturday.
    """
    # Python weekday: Mon=0 ... Sun=6
    wd = d.dt.weekday
    # days since Sunday (Sun -> 0, Mon -> 1, ... Sat -> 6)
    days_since_sun = (wd + 1) % 7
    return d.dt.date - pd.to_timedelta(days_since_sun, unit="D")


def daily_agg(df: pd.DataFrame, dt_col: str = "settled_dt_local") -> pd.DataFrame:
    if dt_col not in df.columns or df.empty:
        return pd.DataFrame(
            columns=["day", "bets", "profit", "avg_profit", "strike_rate"]
        )

    tmp = df.dropna(subset=[dt_col]).copy()
    tmp["day"] = tmp[dt_col].dt.date

    g = tmp.groupby("day", dropna=False)
    out = g.agg(
        bets=("betId", "count") if "betId" in tmp.columns else ("profit", "size"),
        profit=("profit", "sum"),
        avg_profit=("profit", "mean"),
        strike_rate=("is_win", "mean")
        if "is_win" in tmp.columns
        else ("profit", lambda s: (s > 0).mean()),
    ).reset_index()

    out = out.sort_values("day")
    return out


def weekly_agg_sun_start(
    df: pd.DataFrame, dt_col: str = "settled_dt_local"
) -> pd.DataFrame:
    """
    Weekly aggregation where week starts Sunday.
    Output includes week_start (Sunday) and week_end (Saturday).
    """
    if dt_col not in df.columns or df.empty:
        return pd.DataFrame(
            columns=[
                "week_start",
                "week_end",
                "bets",
                "profit",
                "avg_profit",
                "strike_rate",
            ]
        )

    tmp = df.dropna(subset=[dt_col]).copy()
    tmp["week_start"] = _week_start_sunday(tmp[dt_col])
    tmp["week_end"] = tmp["week_start"] + pd.to_timedelta(6, unit="D")

    g = tmp.groupby(["week_start", "week_end"], dropna=False)
    out = g.agg(
        bets=("betId", "count") if "betId" in tmp.columns else ("profit", "size"),
        profit=("profit", "sum"),
        avg_profit=("profit", "mean"),
        strike_rate=("is_win", "mean")
        if "is_win" in tmp.columns
        else ("profit", lambda s: (s > 0).mean()),
    ).reset_index()

    out = out.sort_values("week_start")
    return out


def monthly_agg(df: pd.DataFrame, dt_col: str = "settled_dt_local") -> pd.DataFrame:
    if dt_col not in df.columns or df.empty:
        return pd.DataFrame(
            columns=["month", "bets", "profit", "avg_profit", "strike_rate"]
        )

    tmp = df.dropna(subset=[dt_col]).copy()
    tmp["month"] = tmp[dt_col].dt.to_period("M").astype(str)

    g = tmp.groupby("month", dropna=False)
    out = g.agg(
        bets=("betId", "count") if "betId" in tmp.columns else ("profit", "size"),
        profit=("profit", "sum"),
        avg_profit=("profit", "mean"),
        strike_rate=("is_win", "mean")
        if "is_win" in tmp.columns
        else ("profit", lambda s: (s > 0).mean()),
    ).reset_index()

    # sort by month text works for YYYY-MM
    out = out.sort_values("month")
    return out

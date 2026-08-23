from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from .io import discover_csv_files, load_csv
from .schema import HORSES_LABEL, GREYHOUNDS_LABEL, normalize_cleared_orders_schema

SYDNEY_TZ = ZoneInfo("Australia/Sydney")


@dataclass(frozen=True)
class ProfitBreakdown:
    total_profit: float
    horses_profit: float
    greyhounds_profit: float


@dataclass(frozen=True)
class DailyDmReport:
    report_dt: datetime
    week_start: datetime
    day_start: datetime
    week_to_date: ProfitBreakdown
    day_to_date: ProfitBreakdown
    source_csv: str
    text: str


def _money(value: float) -> str:
    quantized = Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    sign = "-" if quantized < 0 else ""
    return f"{sign}${abs(quantized):,.2f}"


def _format_heading(dt: datetime) -> str:
    """
    Format e.g. ``Saturday 6 June, 9:00 PM`` portably.

    ``%-d``/``%-I`` are glibc-only strftime extensions and fail on Windows,
    so the unpadded day and 12-hour clock are built by hand.
    """
    hour12 = dt.hour % 12 or 12
    meridiem = "AM" if dt.hour < 12 else "PM"
    return f"{dt.strftime('%A')} {dt.day} {dt.strftime('%B')}, {hour12}:{dt.minute:02d} {meridiem}"


def _format_report(report_dt: datetime, week_to_date: ProfitBreakdown, day_to_date: ProfitBreakdown) -> str:
    heading = _format_heading(report_dt)
    lines = [
        "Betfair results update",
        "",
        heading,
        "",
        "Week to date (since Sunday 12:00 AM)",
        f"• Total profit: {_money(week_to_date.total_profit)}",
        f"• Horses: {_money(week_to_date.horses_profit)}",
        f"• Greyhounds: {_money(week_to_date.greyhounds_profit)}",
        "",
        "Today (since 12:00 AM)",
        f"• Total profit: {_money(day_to_date.total_profit)}",
        f"• Horses: {_money(day_to_date.horses_profit)}",
        f"• Greyhounds: {_money(day_to_date.greyhounds_profit)}",
    ]
    return "\n".join(lines)


def _coerce_report_dt(report_dt: datetime | None) -> datetime:
    if report_dt is None:
        return datetime.now(SYDNEY_TZ)
    if report_dt.tzinfo is None:
        return report_dt.replace(tzinfo=SYDNEY_TZ)
    return report_dt.astimezone(SYDNEY_TZ)


def _most_recent_sunday_start(report_dt: datetime) -> datetime:
    day_start = report_dt.replace(hour=0, minute=0, second=0, microsecond=0)
    days_since_sunday = (day_start.weekday() + 1) % 7
    return day_start - timedelta(days=days_since_sunday)


def _profit_breakdown(df: pd.DataFrame) -> ProfitBreakdown:
    if df.empty:
        return ProfitBreakdown(total_profit=0.0, horses_profit=0.0, greyhounds_profit=0.0)

    total_profit = float(df["profit"].sum())
    horses_profit = float(df.loc[df["sport"] == HORSES_LABEL, "profit"].sum())
    greyhounds_profit = float(df.loc[df["sport"] == GREYHOUNDS_LABEL, "profit"].sum())
    return ProfitBreakdown(
        total_profit=total_profit,
        horses_profit=horses_profit,
        greyhounds_profit=greyhounds_profit,
    )


def build_daily_dm_report_from_dataframe(
    df_raw: pd.DataFrame,
    *,
    report_dt: datetime | None = None,
    source_csv: str = "<dataframe>",
) -> DailyDmReport:
    report_dt_local = _coerce_report_dt(report_dt)
    normalized = normalize_cleared_orders_schema(df_raw)
    if "settled_dt_local" not in normalized.columns:
        raise ValueError("Input data could not be normalized with settled_dt_local")

    settled = normalized.dropna(subset=["settled_dt_local"]).copy()
    week_start = _most_recent_sunday_start(report_dt_local)
    day_start = report_dt_local.replace(hour=0, minute=0, second=0, microsecond=0)

    settled = settled.loc[settled["sport"].isin([HORSES_LABEL, GREYHOUNDS_LABEL])]
    settled = settled.loc[settled["settled_dt_local"] <= report_dt_local]

    week_df = settled.loc[settled["settled_dt_local"] >= week_start]
    day_df = settled.loc[settled["settled_dt_local"] >= day_start]

    week_to_date = _profit_breakdown(week_df)
    day_to_date = _profit_breakdown(day_df)
    text = _format_report(report_dt_local, week_to_date, day_to_date)

    return DailyDmReport(
        report_dt=report_dt_local,
        week_start=week_start,
        day_start=day_start,
        week_to_date=week_to_date,
        day_to_date=day_to_date,
        source_csv=source_csv,
        text=text,
    )


def resolve_default_results_csv(results_dir: str) -> Path:
    discovered = discover_csv_files(results_dir)
    if not discovered:
        raise FileNotFoundError(f"No cleared orders CSV files found in: {results_dir}")

    canonical_exact = [
        p for p in discovered if p.name.lower() == "cleared_orders_cleaned.csv"
    ]
    if canonical_exact:
        return canonical_exact[0]

    return discovered[0]


def build_daily_dm_report_from_results_dir(
    results_dir: str,
    *,
    report_dt: datetime | None = None,
    csv_path: str | None = None,
) -> DailyDmReport:
    chosen = Path(csv_path).expanduser() if csv_path else resolve_default_results_csv(results_dir)
    df_raw = load_csv(str(chosen))
    return build_daily_dm_report_from_dataframe(
        df_raw,
        report_dt=report_dt,
        source_csv=str(chosen),
    )

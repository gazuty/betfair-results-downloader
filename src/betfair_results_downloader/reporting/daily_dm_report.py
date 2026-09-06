from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from ..csv_utils import decimal_key
from ..market_status import (
    STATUS_FILENAME,
    is_pending_status,
    load_market_status,
)
from .io import discover_csv_files, load_csv
from .schema import HORSES_LABEL, GREYHOUNDS_LABEL, normalize_cleared_orders_schema

logger = logging.getLogger(__name__)

SYDNEY_TZ = ZoneInfo("Australia/Sydney")

# The two sports the report has always shown. They keep their lines even on
# a quiet day; every other sport appears only when it had a settlement in
# the window, ordered by how much it moved the total.
ALWAYS_SHOWN_SPORTS: tuple[str, ...] = (HORSES_LABEL, GREYHOUNDS_LABEL)


@dataclass(frozen=True)
class ProfitBreakdown:
    total_profit: float
    horses_profit: float
    greyhounds_profit: float
    # (sport label, profit) in display order: the always-shown sports first,
    # then every other sport with rows in the window by absolute profit.
    by_sport: tuple[tuple[str, float], ...] = ()


@dataclass(frozen=True)
class PendingSummary:
    """Markets Betfair has partially settled: money seen, outcome still open."""

    markets: int
    profit: float


@dataclass(frozen=True)
class DailyDmReport:
    report_dt: datetime
    week_start: datetime
    day_start: datetime
    week_to_date: ProfitBreakdown
    day_to_date: ProfitBreakdown
    source_csv: str
    text: str
    hours_stale: float | None = None
    pending: PendingSummary = PendingSummary(markets=0, profit=0.0)
    # The full calendar day before day_start, [yesterday_start, day_start).
    # Independent of the week: on a Sunday it is last week's Saturday.
    yesterday_start: datetime | None = None
    yesterday: ProfitBreakdown | None = None


# The pipeline runs four times a day, so anything older than half a day means
# it has stopped. Without this the report renders a confident $0.00 from a
# stale file and reads exactly like a quiet day.
STALE_AFTER_HOURS = 12.0


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


def _format_age(hours: float) -> str:
    if hours < 48:
        return f"{int(round(hours))}h"
    return f"{int(hours // 24)} days"


def _format_breakdown_lines(breakdown: ProfitBreakdown) -> list[str]:
    lines = [f"• Total profit: {_money(breakdown.total_profit)}"]
    lines.extend(f"• {label}: {_money(profit)}" for label, profit in breakdown.by_sport)
    return lines


def _format_day_name(dt: datetime) -> str:
    """e.g. ``Friday 5 June`` -- portable, unpadded (see _format_heading)."""
    return f"{dt.strftime('%A')} {dt.day} {dt.strftime('%B')}"


def _format_report(
    report_dt: datetime,
    week_to_date: ProfitBreakdown,
    day_to_date: ProfitBreakdown,
    hours_stale: float | None = None,
    pending: PendingSummary | None = None,
    yesterday: ProfitBreakdown | None = None,
    yesterday_start: datetime | None = None,
) -> str:
    heading = _format_heading(report_dt)
    lines = [
        "Betfair results update",
        "",
        heading,
    ]
    if hours_stale is None:
        # No usable settlement timestamp at all: a header-only or unreadable
        # canonical. Treating an unknown age as fresh would deliver a
        # confident $0.00 for a stopped or corrupt pipeline.
        lines.append("")
        lines.append(
            "⚠️ No settled results found — the data may be missing or unreadable"
        )
    elif hours_stale >= STALE_AFTER_HOURS:
        lines.append("")
        lines.append(
            f"⚠️ Data may be stale — newest result is {_format_age(hours_stale)} old"
        )
    lines += ["", "Week to date (since Sunday 12:00 AM)"]
    lines += _format_breakdown_lines(week_to_date)
    if yesterday is not None and yesterday_start is not None:
        # The 6:00 AM report is the first full picture of the previous day;
        # the 7:35 PM one repeats it so the two reports agree.
        lines += ["", f"Yesterday ({_format_day_name(yesterday_start)})"]
        lines += _format_breakdown_lines(yesterday)
    lines += ["", "Today (since 12:00 AM)"]
    lines += _format_breakdown_lines(day_to_date)

    pending = pending or PendingSummary(markets=0, profit=0.0)
    lines += ["", "Pending (partially settled, not counted above)"]
    if pending.markets:
        noun = "market" if pending.markets == 1 else "markets"
        # Deliberately unbounded by the week: a partially settled market's
        # legs accumulate for as long as it stays open, and the whole
        # amount lands in Today on the day it closes.
        lines.append(
            f"• {pending.markets} {noun}, {_money(pending.profit)} settled so far "
            f"— each counts in full on the day it closes"
        )
    else:
        lines.append("• None")
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
        return ProfitBreakdown(
            total_profit=0.0,
            horses_profit=0.0,
            greyhounds_profit=0.0,
            by_sport=tuple((label, 0.0) for label in ALWAYS_SHOWN_SPORTS),
        )

    total_profit = float(df["profit"].sum())
    per_sport = df.groupby("sport", sort=False)["profit"].sum()
    horses_profit = float(per_sport.get(HORSES_LABEL, 0.0))
    greyhounds_profit = float(per_sport.get(GREYHOUNDS_LABEL, 0.0))

    by_sport: list[tuple[str, float]] = [
        (label, float(per_sport.get(label, 0.0))) for label in ALWAYS_SHOWN_SPORTS
    ]
    others = [
        (str(label), float(profit))
        for label, profit in per_sport.items()
        if label not in ALWAYS_SHOWN_SPORTS
    ]
    others.sort(key=lambda item: (-abs(item[1]), item[0]))
    by_sport.extend(others)

    return ProfitBreakdown(
        total_profit=total_profit,
        horses_profit=horses_profit,
        greyhounds_profit=greyhounds_profit,
        by_sport=tuple(by_sport),
    )


def apply_settlement_status(
    normalized: pd.DataFrame,
    market_status: pd.DataFrame | None,
    *,
    as_of: datetime | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    Split normalized rows into ``(final, pending)`` using the market status
    file written by the pipeline (see :mod:`..market_status`).

    - A market recorded as anything but CLOSED is pending: its rows are held
      out of every total.
    - A market recorded CLOSED that was earlier seen pending has its rows'
      ``settled_dt_local`` moved to the moment the close was observed, so
      the whole market counts on the day it finished instead of its legs
      being scattered across weeks that were already reported. If that
      moment is after ``as_of`` (a report re-rendered for an earlier time),
      the market was still pending *as of then*, and is reported that way.
    - A market with no record counts as final, unchanged. That is exactly
      today's behaviour, so an outage in the status step degrades to the
      old report rather than to a zero one.

    Markets match on the numeric key so the float-damaged spellings in the
    historical canonical still find their status row. A status row without
    a marketId is ignored: it could otherwise hold back every canonical row
    whose own marketId is blank.
    """
    if (
        market_status is None
        or market_status.empty
        or "marketId" not in normalized.columns
    ):
        return normalized, normalized.iloc[0:0]

    status = market_status.copy()
    status["marketId"] = status["marketId"].fillna("").astype(str).str.strip()
    status = status[status["marketId"] != ""]
    if status.empty:
        return normalized, normalized.iloc[0:0]
    status["_key"] = status["marketId"].map(decimal_key)
    # The pipeline merges on this key, so duplicates only arise from a
    # hand-edited file; the most recent row is the one Betfair said last.
    status = status.drop_duplicates(subset=["_key"], keep="last")
    status["_pending"] = status["status"].map(is_pending_status)
    closed_at = pd.to_datetime(
        status["closedObservedUtc"], utc=True, errors="coerce", format="ISO8601"
    )
    was_pending = status["firstPendingUtc"].fillna("").astype(str).str.len() > 0
    # Only a market that actually went through a pending phase is re-dated;
    # racing and match markets that were CLOSED at first sight keep their
    # settledDate exactly as before.
    status["_close_local"] = closed_at.where(~status["_pending"] & was_pending)
    status["_close_local"] = status["_close_local"].dt.tz_convert(SYDNEY_TZ)
    if as_of is not None:
        not_yet = status["_close_local"].notna() & (status["_close_local"] > as_of)
        status.loc[not_yet, "_pending"] = True
        status.loc[not_yet, "_close_local"] = pd.NaT

    keys = normalized["marketId"].map(decimal_key)
    lookup = status.set_index("_key")
    pending_mask = keys.map(lookup["_pending"]).fillna(False).astype(bool)
    close_local = keys.map(lookup["_close_local"])

    final = normalized.loc[~pending_mask].copy()
    redate = close_local.loc[final.index].notna()
    if redate.any():
        moved = close_local.loc[final.index][redate]
        final.loc[redate, "settled_dt_local"] = moved
        if "settled_date_local" in final.columns:
            final.loc[redate, "settled_date_local"] = moved.dt.date
    pending = normalized.loc[pending_mask].copy()
    return final, pending


def build_daily_dm_report_from_dataframe(
    df_raw: pd.DataFrame,
    *,
    report_dt: datetime | None = None,
    source_csv: str = "<dataframe>",
    market_status: pd.DataFrame | None = None,
) -> DailyDmReport:
    report_dt_local = _coerce_report_dt(report_dt)
    normalized = normalize_cleared_orders_schema(df_raw)
    if "settled_dt_local" not in normalized.columns:
        raise ValueError("Input data could not be normalized with settled_dt_local")

    settled = normalized.dropna(subset=["settled_dt_local"]).copy()

    # Freshness is measured here, before any filtering below. It answers
    # "is the pipeline still delivering", not "have my sports run lately" --
    # a quiet day for horses and greyhounds while other event types settle
    # normally is not a stalled pipeline, and a pending outright's early
    # legs are deliveries too.
    # Rows settled after the report timestamp are excluded here for the same
    # reason the profit totals exclude them: with --at on a historical
    # timestamp, a later settlement would otherwise make the report look fresh
    # while the numbers it shows are hours old.
    in_scope = settled.loc[settled["settled_dt_local"] <= report_dt_local]
    hours_stale: float | None = None
    if not in_scope.empty:
        newest_any_sport = in_scope["settled_dt_local"].max()
        hours_stale = max(
            (report_dt_local - newest_any_sport).total_seconds() / 3600.0, 0.0
        )

    week_start = _most_recent_sunday_start(report_dt_local)
    day_start = report_dt_local.replace(hour=0, minute=0, second=0, microsecond=0)

    final, pending_rows = apply_settlement_status(
        settled, market_status, as_of=report_dt_local
    )
    final = final.loc[final["settled_dt_local"] <= report_dt_local]
    pending_rows = pending_rows.loc[pending_rows["settled_dt_local"] <= report_dt_local]

    yesterday_start = day_start - timedelta(days=1)

    week_df = final.loc[final["settled_dt_local"] >= week_start]
    day_df = final.loc[final["settled_dt_local"] >= day_start]
    yesterday_df = final.loc[
        (final["settled_dt_local"] >= yesterday_start)
        & (final["settled_dt_local"] < day_start)
    ]

    week_to_date = _profit_breakdown(week_df)
    day_to_date = _profit_breakdown(day_df)
    yesterday = _profit_breakdown(yesterday_df)
    pending = PendingSummary(
        markets=int(pending_rows["marketId"].map(decimal_key).nunique())
        if not pending_rows.empty
        else 0,
        profit=float(pending_rows["profit"].sum()) if not pending_rows.empty else 0.0,
    )

    text = _format_report(
        report_dt_local,
        week_to_date,
        day_to_date,
        hours_stale,
        pending,
        yesterday,
        yesterday_start,
    )

    return DailyDmReport(
        report_dt=report_dt_local,
        week_start=week_start,
        day_start=day_start,
        week_to_date=week_to_date,
        day_to_date=day_to_date,
        source_csv=source_csv,
        text=text,
        hours_stale=hours_stale,
        pending=pending,
        yesterday_start=yesterday_start,
        yesterday=yesterday,
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


def resolve_market_status_path(csv_path: Path) -> Path:
    """The status file the pipeline writes next to the canonical it read."""
    return Path(csv_path).parent / ".cache" / STATUS_FILENAME


def load_market_status_for_report(csv_path: Path) -> pd.DataFrame | None:
    """
    Load the status file beside ``csv_path``; ``None`` when there is none.

    An unreadable file is logged and treated as absent: the report then
    counts every market as final, which is the pre-feature behaviour, and
    the pipeline's own run will refuse to overwrite the damaged file.
    """
    path = resolve_market_status_path(csv_path)
    if not path.exists():
        return None
    try:
        return load_market_status(path)
    except Exception as exc:
        logger.warning(
            "Could not read market status file %s (%s: %s); reporting every "
            "market as fully settled.",
            path,
            type(exc).__name__,
            exc,
        )
        return None


def supplement_with_archived_legs(
    df_raw: pd.DataFrame,
    market_status: pd.DataFrame | None,
    results_dir: Path,
) -> pd.DataFrame:
    """
    Add archived legs of every market that was ever seen pending.

    A market can stay partially settled for longer than
    ``user.canonical_archive_months``; its early legs are then moved into
    the yearly archives while the report reads only the rolling canonical.
    The Pending amount would understate, and the day the market closed would
    count only the legs still in the canonical instead of the whole market.
    Only markets with a ``firstPendingUtc`` are looked up, and only rows the
    canonical does not already hold are added. With no archives on disk
    this is a directory glob and nothing more.
    """
    if market_status is None or market_status.empty:
        return df_raw
    if "marketId" not in market_status.columns or "betId" not in df_raw.columns:
        return df_raw
    ids = market_status["marketId"].fillna("").astype(str).str.strip()
    was_pending = market_status["firstPendingUtc"].fillna("").astype(str).str.len() > 0
    interest = ids[was_pending & (ids != "")]
    if interest.empty:
        return df_raw

    from ..downloader_core import (  # noqa: PLC0415
        _archived_rows_for_markets,
        _rows_not_already_held,
    )

    keys = set(interest.map(decimal_key))
    extras = [
        extra
        for archived in _archived_rows_for_markets(results_dir, keys)
        for extra in [_rows_not_already_held(df_raw, archived)]
        if len(extra)
    ]
    if not extras:
        return df_raw
    return pd.concat([df_raw, *extras], ignore_index=True)


def build_daily_dm_report_from_results_dir(
    results_dir: str,
    *,
    report_dt: datetime | None = None,
    csv_path: str | None = None,
) -> DailyDmReport:
    chosen = (
        Path(csv_path).expanduser()
        if csv_path
        else resolve_default_results_csv(results_dir)
    )
    df_raw = load_csv(str(chosen))
    market_status = load_market_status_for_report(chosen)
    df_raw = supplement_with_archived_legs(df_raw, market_status, chosen.parent)
    return build_daily_dm_report_from_dataframe(
        df_raw,
        report_dt=report_dt,
        source_csv=str(chosen),
        market_status=market_status,
    )

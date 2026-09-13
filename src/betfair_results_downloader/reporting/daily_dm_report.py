from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from ..commission import COMMISSION_FILENAME, load_market_commission
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
class SportFigures:
    """Gross, commission and net for one sport (or the total) in a window."""

    label: str
    gross: float
    commission: float
    # Markets in the window with no trustworthy commission figure: no row in
    # the commission store, or a row read before the market closed. Their
    # commission counts as $0.00 and the section says so.
    unknown_markets: int = 0

    @property
    def net(self) -> float:
        return self.gross - self.commission

    @property
    def commission_pct(self) -> float | None:
        """
        Commission as a percentage of gross profit for the window. ``None``
        when gross is zero or a loss (Betfair charges on winning markets
        only, so the ratio has no meaning for a losing period) and when any
        market's commission is unknown (a ratio over a partly missing
        numerator would read as a real rate). Over a day the ratio is also
        distorted by markets whose legs straddle midnight, since gross is
        per leg and commission lands with the latest leg; the month and
        year sections are where it settles to the effective rate.
        """
        if self.gross <= 0 or self.unknown_markets:
            return None
        return 100.0 * self.commission / self.gross


@dataclass(frozen=True)
class ProfitBreakdown:
    total: SportFigures
    # Display order: the always-shown sports first, then every other sport
    # with rows in the window by absolute gross profit.
    by_sport: tuple[SportFigures, ...] = ()

    @property
    def total_profit(self) -> float:
        """Gross profit before commission, as the report showed until 0.8.0."""
        return self.total.gross

    @property
    def horses_profit(self) -> float:
        return self._gross_for(HORSES_LABEL)

    @property
    def greyhounds_profit(self) -> float:
        return self._gross_for(GREYHOUNDS_LABEL)

    def _gross_for(self, label: str) -> float:
        for figures in self.by_sport:
            if figures.label == label:
                return figures.gross
        return 0.0


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
    # Commission is only meaningful over a period, so the report also shows
    # the calendar month and year to date (Sydney time).
    month_start: datetime | None = None
    month_to_date: ProfitBreakdown | None = None
    year_start: datetime | None = None
    year_to_date: ProfitBreakdown | None = None


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


def _format_pct(pct: float | None) -> str:
    return "n/a" if pct is None else f"{pct:.1f}%"


def _format_figures_line(figures: SportFigures) -> str:
    return (
        f"• {figures.label}: gross {_money(figures.gross)}, "
        f"commission {_money(figures.commission)} "
        f"({_format_pct(figures.commission_pct)}), net {_money(figures.net)}"
    )


def _format_breakdown_lines(breakdown: ProfitBreakdown) -> list[str]:
    lines = [_format_figures_line(breakdown.total)]
    lines.extend(_format_figures_line(figures) for figures in breakdown.by_sport)
    unknown = breakdown.total.unknown_markets
    if unknown:
        noun = "market" if unknown == 1 else "markets"
        # Said out loud rather than folded into the total: a missing store
        # or a pre-close placeholder would otherwise read as commission-free.
        lines.append(f"• ⚠️ Commission unknown for {unknown} {noun} (counted as $0.00)")
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
    month_to_date: ProfitBreakdown | None = None,
    month_start: datetime | None = None,
    year_to_date: ProfitBreakdown | None = None,
    year_start: datetime | None = None,
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
    if month_to_date is not None and month_start is not None:
        lines += ["", f"Month to date (since {_format_day_name(month_start)})"]
        lines += _format_breakdown_lines(month_to_date)
    if year_to_date is not None and year_start is not None:
        lines += ["", f"Year to date (since {_format_day_name(year_start)})"]
        lines += _format_breakdown_lines(year_to_date)

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


def _empty_breakdown() -> ProfitBreakdown:
    return ProfitBreakdown(
        total=SportFigures("Total", 0.0, 0.0),
        by_sport=tuple(SportFigures(label, 0.0, 0.0) for label in ALWAYS_SHOWN_SPORTS),
    )


def _profit_breakdown(df: pd.DataFrame, markets: pd.DataFrame) -> ProfitBreakdown:
    """
    Gross from the bet rows in the window, commission from the markets whose
    latest leg falls in it (see :func:`market_commission_frame`).
    """
    if df.empty:
        return _empty_breakdown()

    gross_by_sport = df.groupby("sport", sort=False)["profit"].sum()
    if markets.empty:
        commission_by_sport = pd.Series(dtype=float)
        unknown_by_sport = pd.Series(dtype=int)
    else:
        commission_by_sport = markets.groupby("sport", sort=False)["commission"].sum()
        unknown_by_sport = markets.groupby("sport", sort=False)["unknown"].sum()

    def figures(label: str) -> SportFigures:
        return SportFigures(
            label=label,
            gross=float(gross_by_sport.get(label, 0.0)),
            commission=float(commission_by_sport.get(label, 0.0)),
            unknown_markets=int(unknown_by_sport.get(label, 0)),
        )

    by_sport: list[SportFigures] = [figures(label) for label in ALWAYS_SHOWN_SPORTS]
    others = [
        figures(str(label))
        for label in gross_by_sport.index
        if label not in ALWAYS_SHOWN_SPORTS
    ]
    others.sort(key=lambda item: (-abs(item.gross), item.label))
    by_sport.extend(others)

    total = SportFigures(
        label="Total",
        gross=float(df["profit"].sum()),
        commission=float(markets["commission"].sum()) if not markets.empty else 0.0,
        unknown_markets=int(markets["unknown"].sum()) if not markets.empty else 0,
    )
    return ProfitBreakdown(total=total, by_sport=tuple(by_sport))


def market_commission_frame(
    final: pd.DataFrame,
    market_commission: pd.DataFrame | None,
    market_status: pd.DataFrame | None,
    *,
    as_of: datetime | None = None,
) -> pd.DataFrame:
    """
    One row per market in ``final``: its sport, the local time that decides
    which window its commission belongs to, the commission read for it, and
    whether that figure is trustworthy.

    Commission is placed on the store row's own ``settledDateUtc`` -- the
    latest leg Betfair had seen when the row was read -- not on the latest
    canonical row. The two differ exactly when the report is rendered for
    an earlier time (``--at``): the canonical rows after the cutoff are
    gone, so the latest remaining leg is an earlier one, and the store's
    final figure would otherwise land on it. A market that was seen pending
    and later CLOSED is the exception: its rows were re-dated to the close
    (see :func:`apply_settlement_status`), so its commission follows them.

    A market is ``unknown`` when the store has no row for it, the amount
    is unparseable, or the row shows zero commission against a positive
    gross (a row with no marketId at all is always in this case); when it was ever seen pending and the row was read
    before the close was observed (a partially settled market reports 0.0
    until it closes); when the store row predates a leg the canonical
    already holds (a stale read); or when the store row's settlement is
    after ``as_of``, because the figure did not exist at that time. Unknown
    markets contribute $0.00, sit in the window of their gross, and are
    counted so the report can say so.
    """
    columns = ["_key", "sport", "last_settled_local", "commission", "unknown"]
    if final.empty or "marketId" not in final.columns:
        return pd.DataFrame(columns=columns)
    # A row with no marketId still counts in gross but can never match a
    # store row; each such row stands as its own market so it is reported
    # commission unknown rather than silently commission-free.
    ids = final["marketId"].fillna("").astype(str).str.strip()
    keys = [
        decimal_key(mid) if mid else f"blank:{pos}"
        for pos, mid in enumerate(ids.tolist())
    ]
    keyed = final.assign(_key=keys)
    markets = (
        keyed.groupby("_key", sort=False)
        .agg(sport=("sport", "first"), rows_latest=("settled_dt_local", "max"))
        .reset_index()
    )

    # Plain dicts rather than Series lookups: mapping onto an empty
    # datetime Series raises in pandas, and a dict is indifferent to dtype.
    commission: dict[str, float] = {}
    fetched: dict[str, pd.Timestamp] = {}
    store_settled: dict[str, pd.Timestamp] = {}
    if market_commission is not None and not market_commission.empty:
        store = market_commission.copy()
        store["_key"] = store["marketId"].fillna("").astype(str).map(decimal_key)
        store = store[store["_key"] != ""].drop_duplicates(subset=["_key"], keep="last")
        amounts = pd.to_numeric(store["commission"], errors="coerce")
        gross = pd.to_numeric(store["grossProfit"], errors="coerce")
        read_at = pd.to_datetime(
            store["fetchedUtc"], utc=True, errors="coerce", format="ISO8601"
        )
        settled_at = pd.to_datetime(
            store["settledDateUtc"], utc=True, errors="coerce", format="ISO8601"
        )
        for key, amount, won, ts, settled in zip(
            store["_key"], amounts, gross, read_at, settled_at
        ):
            # Betfair charges on every winning market, so a zero against a
            # positive gross is the pre-close placeholder of a market the
            # status step never saw pending (see commission); the seed
            # re-reads it, and until then it is not a figure.
            placeholder = amount == 0 and pd.notna(won) and won > 0
            if pd.notna(amount) and not placeholder:
                commission[key] = float(amount)
            if pd.notna(ts):
                fetched[key] = ts
            if pd.notna(settled):
                store_settled[key] = settled.tz_convert(SYDNEY_TZ)

    closed_for: dict[str, pd.Timestamp] = {}
    if market_status is not None and not market_status.empty:
        status = market_status.copy()
        status["_key"] = status["marketId"].fillna("").astype(str).map(decimal_key)
        status = status[status["_key"] != ""].drop_duplicates(
            subset=["_key"], keep="last"
        )
        was_pending = status["firstPendingUtc"].fillna("").astype(str).str.len() > 0
        closed_at = pd.to_datetime(
            status["closedObservedUtc"], utc=True, errors="coerce", format="ISO8601"
        )
        closed_for = {
            key: ts
            for key, ts, pending in zip(status["_key"], closed_at, was_pending)
            if pending and pd.notna(ts)
        }

    placements: list[pd.Timestamp] = []
    amounts_out: list[float] = []
    unknown_out: list[bool] = []
    for key, rows_latest in zip(markets["_key"], markets["rows_latest"]):
        amount = commission.get(key)
        placement = rows_latest
        unknown = amount is None
        if key in closed_for:
            # Re-dated to the close by apply_settlement_status; a read taken
            # before the close still holds Betfair's 0.0 placeholder.
            if key not in fetched or fetched[key] < closed_for[key]:
                unknown = True
        elif key in store_settled:
            settled = store_settled[key]
            if settled < rows_latest:
                # The canonical holds a leg the store row never saw.
                unknown = True
            elif as_of is not None and settled > as_of:
                # The figure belongs after this report's cutoff.
                unknown = True
            else:
                placement = settled
        placements.append(placement)
        amounts_out.append(0.0 if unknown else float(amount))
        unknown_out.append(unknown)

    markets["last_settled_local"] = placements
    markets["commission"] = amounts_out
    markets["unknown"] = unknown_out
    return markets[columns]


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
    market_commission: pd.DataFrame | None = None,
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
    month_start = day_start.replace(day=1)
    year_start = day_start.replace(month=1, day=1)

    markets = market_commission_frame(
        final, market_commission, market_status, as_of=report_dt_local
    )

    def breakdown(start: datetime, end: datetime | None = None) -> ProfitBreakdown:
        rows = final.loc[final["settled_dt_local"] >= start]
        mk = (
            markets.loc[markets["last_settled_local"] >= start]
            if not markets.empty
            else markets
        )
        if end is not None:
            rows = rows.loc[rows["settled_dt_local"] < end]
            if not mk.empty:
                mk = mk.loc[mk["last_settled_local"] < end]
        return _profit_breakdown(rows, mk)

    week_to_date = breakdown(week_start)
    day_to_date = breakdown(day_start)
    yesterday = breakdown(yesterday_start, day_start)
    month_to_date = breakdown(month_start)
    year_to_date = breakdown(year_start)
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
        month_to_date,
        month_start,
        year_to_date,
        year_start,
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
        month_start=month_start,
        month_to_date=month_to_date,
        year_start=year_start,
        year_to_date=year_to_date,
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


def resolve_market_commission_path(csv_path: Path) -> Path:
    """The commission store the pipeline writes next to the canonical it read."""
    return Path(csv_path).parent / ".cache" / COMMISSION_FILENAME


def load_market_commission_for_report(csv_path: Path) -> pd.DataFrame | None:
    """
    Load the commission store beside ``csv_path``; ``None`` when there is
    none. An unreadable file is logged and treated as absent: every market
    is then commission-unknown, which the report says out loud, rather than
    silently commission-free.
    """
    path = resolve_market_commission_path(csv_path)
    if not path.exists():
        return None
    try:
        return load_market_commission(path)
    except Exception as exc:
        logger.warning(
            "Could not read market commission file %s (%s: %s); reporting "
            "every market as commission unknown.",
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


def supplement_with_archived_year(
    df_raw: pd.DataFrame,
    results_dir: Path,
    year_start: datetime,
) -> pd.DataFrame:
    """
    Add every archived row settled on or after ``year_start``.

    The canonical is rolling (``user.canonical_archive_months``); with a
    window shorter than the months elapsed this year, ordinary markets from
    earlier in the year sit only in the yearly archives, and Year to date
    would silently understate. Only archives for years the window touches
    are read, and only rows the canonical does not already hold are added.
    With no archives on disk this is a directory glob and nothing more. An
    unreadable archive is logged and skipped, matching the pending-leg
    supplement.
    """
    if "betId" not in df_raw.columns:
        return df_raw
    try:
        paths = sorted(Path(results_dir).glob("cleared_orders_archive_*.csv.gz"))
    except OSError:
        return df_raw
    since = pd.Timestamp(year_start.astimezone(SYDNEY_TZ)).tz_convert("UTC")
    extras: list[pd.DataFrame] = []
    from ..downloader_core import _rows_not_already_held  # noqa: PLC0415

    for path in paths:
        try:
            year = int(path.stem.rsplit("_", 1)[-1].split(".")[0])
        except ValueError:
            year = since.year
        if year < since.year:
            continue
        try:
            archived = pd.read_csv(path, dtype=str, keep_default_na=False)
        except (OSError, ValueError) as exc:
            logger.warning(
                "Could not read archive %s for Year to date (%s: %s); its rows "
                "are missing from that section.",
                path.name,
                type(exc).__name__,
                exc,
            )
            continue
        if archived.empty or "settledDate" not in archived.columns:
            continue
        settled = pd.to_datetime(
            archived["settledDate"], utc=True, errors="coerce", format="ISO8601"
        )
        in_year = archived.loc[settled >= since]
        if in_year.empty:
            continue
        extra = _rows_not_already_held(df_raw, in_year)
        if len(extra):
            extras.append(extra)
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
    market_commission = load_market_commission_for_report(chosen)
    df_raw = supplement_with_archived_legs(df_raw, market_status, chosen.parent)
    report_dt_local = _coerce_report_dt(report_dt)
    year_start = report_dt_local.replace(
        month=1, day=1, hour=0, minute=0, second=0, microsecond=0
    )
    df_raw = supplement_with_archived_year(df_raw, chosen.parent, year_start)
    return build_daily_dm_report_from_dataframe(
        df_raw,
        report_dt=report_dt,
        source_csv=str(chosen),
        market_status=market_status,
        market_commission=market_commission,
    )

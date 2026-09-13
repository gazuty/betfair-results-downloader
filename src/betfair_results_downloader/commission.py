"""
Per-market commission: what Betfair actually charged on each market.

The per-bet rows the downloader stores carry no commission at all -- Betfair
charges it per market, on the net winnings of the whole market, so there is
no per-bet figure to store. ``listClearedOrders`` with ``groupBy=MARKET``
returns one row per market with ``profit`` (gross: it equals the sum of the
per-bet profits exactly) and ``commission`` (the whole-market charge, 0.0 on
a losing market). Net for a market is gross minus commission. Three facts
about that endpoint, verified live on 2026-09-13, shape everything here:

- The grouped row sits at the market's *latest* settled leg: a window that
  covers only earlier legs returns nothing for the market. So a run window
  that contains a market's latest leg sees the whole market, and a market
  whose legs keep settling reappears in later windows with larger totals.
  The store therefore upserts by market and the latest read wins.
- A partially settled market reports commission 0.0 with a cumulative gross
  until it closes. A 0.0 read before the close is not the answer, only a
  placeholder, so every market that was ever seen pending (see
  :mod:`market_status`) is asked about again, by explicit id, until a read
  has been taken after the observed close.
- Grouped rows are available for about a year back, the same depth as the
  per-bet rows, so the whole canonical can be backfilled.

Commission rates vary by market and are not recoverable once the market has
left the catalogue; the store keeps the amount charged and the report
derives the effective percentage at the period and sport level, never per
market.

Observations live in ``<results_csv_dir>/.cache/market_commission.csv``
keyed by marketId (see :data:`COMMISSION_COLUMNS`).
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

import pandas as pd

from .betfair_net import chunked, retry_betfair_call
from .csv_utils import decimal_key
from .market_status import (
    _cell,
    _fmt_utc,
    _now_utc,
    _parse_utc,
    is_pending_status,
)

logger = logging.getLogger(__name__)

COMMISSION_FILENAME = "market_commission.csv"

COMMISSION_COLUMNS: list[str] = [
    "marketId",
    "eventTypeId",
    # Betfair's settledDate on the grouped row: the market's latest leg.
    "settledDateUtc",
    "betCount",
    # Sum of the per-bet profits, as Betfair aggregates them.
    "grossProfit",
    # The whole-market charge; 0.0 on a losing or still-pending market.
    "commission",
    # When this row was read. Compared with the status file's
    # closedObservedUtc to tell a pre-close placeholder from a final figure.
    "fetchedUtc",
]

# Betfair's page cap for listClearedOrders.
DEFAULT_PAGE_SIZE = 200

# Explicit-id re-queries still need a settledDateRange. Grouped rows are
# only served for about a year, so asking further back finds nothing; the
# range is sent whole rather than in date chunks because the id filter, not
# the range, bounds the rows returned.
DEFAULT_REQUERY_LOOKBACK_DAYS = 365

# Self-heal after a failed step, and the first-run seed: canonical markets
# settled within this many days that have no row in the store yet are read
# by explicit id on the next run. A window the step missed (Betfair down at
# 06:00) would otherwise stay commission-unknown for good, because the
# scheduler's next window never touches those markets again. Report windows
# look back a week; a fortnight leaves room for a run or two to fail.
DEFAULT_RECENT_DAYS = 14

# Ceiling on that seed per run, newest settlements first: a first run after
# deploy on a year-old canonical must not stall the pipeline. The rest is
# picked up on later runs, or all at once by ``backfill-commission``.
DEFAULT_MAX_RECENT_UNKNOWN = 2_000

# The store is never pruned: the report's Year to date section reads a
# full year of it, and a year of markets is a few megabytes of CSV.

# Ids per explicit re-query call. Only ever-pending markets are re-queried
# (a handful at a time), so this is a ceiling, not a tuning knob.
DEFAULT_REQUERY_BATCH = 50


@dataclass(frozen=True)
class MarketCommission:
    market_id: str
    event_type_id: str
    settled_date_utc: str
    bet_count: int
    gross_profit: float
    commission: float


@dataclass(frozen=True)
class CommissionResult:
    attempted: bool
    fetched: int
    requeried: int
    message: str
    path: Optional[Path] = None


# -----------------------------
# Persistence
# -----------------------------


def resolve_commission_path(cache_dir: Path) -> Path:
    return Path(cache_dir) / COMMISSION_FILENAME


def empty_commission_frame() -> pd.DataFrame:
    return pd.DataFrame({c: pd.Series(dtype=str) for c in COMMISSION_COLUMNS})


def load_market_commission(path: Path) -> pd.DataFrame:
    """
    Load the commission file as strings. A missing file is an empty frame;
    an unreadable one raises so the caller decides -- the runner must not
    overwrite a corrupt file with one that has forgotten a year of charges.
    """
    path = Path(path)
    if not path.exists():
        return empty_commission_frame()
    # dtype=str for the same reason as the canonical: inferred types would
    # rewrite marketId "1.251500100" as 1.2515001.
    df = pd.read_csv(path, dtype=str, keep_default_na=False)
    for col in COMMISSION_COLUMNS:
        if col not in df.columns:
            df[col] = ""
    return df[COMMISSION_COLUMNS]


def save_market_commission(df: pd.DataFrame, path: Path) -> None:
    """Write via a temp file and rename so a crash cannot leave a torn file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(path.name + ".tmp")
    try:
        df.reindex(columns=COMMISSION_COLUMNS).to_csv(tmp_path, index=False)
        tmp_path.replace(path)
    finally:
        if tmp_path.exists():
            tmp_path.unlink()


# -----------------------------
# Betfair: listClearedOrders groupBy=MARKET
# -----------------------------


def _parse_grouped_row(row: Any) -> Optional[MarketCommission]:
    """
    Turn one grouped-by-market response row into a record.

    Returns ``None`` for a row without a usable marketId. An unparseable
    amount raises: a silently zeroed market would read as commission-free,
    wrong in a way nobody would notice. A row that omits ``commission``
    altogether is taken as 0.0 only when the market lost (Betfair charges
    nothing on a loss); on a winning market the row is skipped with a
    warning, leaving the market commission-unknown for the recent-market
    seed to read again, rather than recording a trusted $0.00.
    """
    if not isinstance(row, dict):
        raise TypeError(
            f"Unexpected clearedOrders row type {type(row).__name__}; expected dict."
        )
    market_id = str(row.get("marketId") or "").strip()
    if not market_id:
        return None
    try:
        gross = float(row.get("profit", 0.0) or 0.0)
        bet_count = int(row.get("betCount", 0) or 0)
        raw_commission = row.get("commission")
        if raw_commission is None or raw_commission == "":
            if gross > 0:
                logger.warning(
                    "Market %s: winning grouped row carries no commission; "
                    "skipped so it stays commission-unknown.",
                    market_id,
                )
                return None
            commission = 0.0
        else:
            commission = float(raw_commission)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"Market {market_id}: unparseable grouped amounts {row!r}"
        ) from exc
    return MarketCommission(
        market_id=market_id,
        event_type_id=str(row.get("eventTypeId") or "").strip(),
        settled_date_utc=str(row.get("settledDate") or "").strip(),
        bet_count=bet_count,
        gross_profit=gross,
        commission=commission,
    )


def fetch_grouped_markets(
    client: Any,
    from_dt: datetime,
    to_dt: datetime,
    *,
    market_ids: Optional[list[str]] = None,
    page_size: int = DEFAULT_PAGE_SIZE,
    max_retries: int = 5,
) -> list[MarketCommission]:
    """
    Every grouped-by-market row whose latest leg settled in ``[from_dt,
    to_dt)``, optionally restricted to ``market_ids``. Pages until Betfair
    says there is no more, with the same runaway cap as the bet download.
    """
    settled_range = {"from": _fmt_utc(from_dt), "to": _fmt_utc(to_dt)}
    rows: list[MarketCommission] = []
    from_record = 0
    max_pages = 10_000
    for _page in range(max_pages):
        kwargs: dict[str, Any] = dict(
            bet_status="SETTLED",
            settled_date_range=settled_range,
            group_by="MARKET",
            from_record=from_record,
            record_count=page_size,
            lightweight=True,
        )
        if market_ids:
            kwargs["market_ids"] = list(market_ids)
        data = retry_betfair_call(
            lambda kwargs=kwargs: client.betting.list_cleared_orders(**kwargs),
            max_attempts=max_retries,
        )
        if not isinstance(data, dict):
            raise TypeError(
                f"Unexpected listClearedOrders response type "
                f"{type(data).__name__}; expected dict."
            )
        batch = data.get("clearedOrders") or []
        if not batch:
            break
        for raw in batch:
            parsed = _parse_grouped_row(raw)
            if parsed is not None:
                rows.append(parsed)
        from_record += page_size
        if data.get("moreAvailable") is False:
            break
    else:
        raise RuntimeError(
            f"Grouped cleared-orders pagination exceeded {max_pages:,} pages; "
            "aborting rather than looping unattended."
        )
    return rows


def fetch_commission_for_window(
    client: Any,
    from_dt: datetime,
    to_dt: datetime,
    *,
    chunk_days: int = 30,
    status_cb: Optional[Callable[[str], None]] = None,
    sleep_seconds: float = 0.2,
    sleep: Callable[[float], Any] = time.sleep,
) -> list[MarketCommission]:
    """Grouped rows for a window, in the same date chunks as the bet download."""
    from .downloader_core import _build_datetime_chunks  # noqa: PLC0415

    chunks = _build_datetime_chunks(from_dt, to_dt, chunk_days)
    rows: list[MarketCommission] = []
    for idx, (c_from, c_to) in enumerate(chunks, start=1):
        if idx > 1:
            sleep(sleep_seconds)
        if status_cb and len(chunks) > 1:
            status_cb(
                f"Commission chunk {idx}/{len(chunks)}: "
                f"{c_from:%Y-%m-%d %H:%M} -> {c_to:%Y-%m-%d %H:%M} (exclusive)"
            )
        rows.extend(fetch_grouped_markets(client, c_from, c_to))
    return rows


def fetch_commission_for_markets(
    client: Any,
    market_ids: Iterable[str],
    *,
    now: Optional[datetime] = None,
    lookback_days: int = DEFAULT_REQUERY_LOOKBACK_DAYS,
    batch_size: int = DEFAULT_REQUERY_BATCH,
    sleep_seconds: float = 0.2,
    sleep: Callable[[float], Any] = time.sleep,
) -> list[MarketCommission]:
    """Grouped rows for explicit ids, whatever window their latest leg is in."""
    requested = [str(m).strip() for m in market_ids if str(m).strip()]
    if not requested:
        return []
    now_utc = _now_utc(now)
    from_dt = now_utc - timedelta(days=lookback_days)
    rows: list[MarketCommission] = []
    for idx, batch in enumerate(chunked(requested, batch_size)):
        if idx:
            sleep(sleep_seconds)
        rows.extend(fetch_grouped_markets(client, from_dt, now_utc, market_ids=batch))
    return rows


# -----------------------------
# Selection and merge
# -----------------------------


def select_requery_markets(
    df_status: Optional[pd.DataFrame],
    df_commission: pd.DataFrame,
) -> list[str]:
    """
    Markets whose stored commission cannot yet be trusted: every market the
    status file has ever seen pending that is either still pending, has no
    commission row, or whose row was read before the close was observed.
    A market CLOSED at first sight (racing, match odds) is never here; its
    first read is final.
    """
    if (
        df_status is None
        or df_status.empty
        or "firstPendingUtc" not in df_status
        or "closedObservedUtc" not in df_status
    ):
        return []
    ever_pending = df_status[df_status["firstPendingUtc"].map(_cell) != ""]
    if ever_pending.empty:
        return []

    fetched_by_key: dict[str, pd.Timestamp] = {}
    if df_commission is not None and not df_commission.empty:
        fetched = _parse_utc(df_commission["fetchedUtc"])
        for key, ts in zip(df_commission["marketId"].map(decimal_key), fetched):
            fetched_by_key[key] = ts

    closed_at = _parse_utc(ever_pending["closedObservedUtc"])
    result: list[str] = []
    for (_, rec), closed in zip(ever_pending.iterrows(), closed_at):
        mid = _cell(rec.get("marketId")).strip()
        if not mid:
            continue
        if is_pending_status(rec.get("status")):
            result.append(mid)
            continue
        fetched_at = fetched_by_key.get(decimal_key(mid))
        if fetched_at is None or pd.isna(fetched_at):
            result.append(mid)
        elif pd.isna(closed) or fetched_at < closed:
            result.append(mid)
    return result


def select_recent_unknown_markets(
    df_canonical: Optional[pd.DataFrame],
    df_commission: pd.DataFrame,
    *,
    now: Optional[datetime] = None,
    recent_days: int = DEFAULT_RECENT_DAYS,
    max_markets: int = DEFAULT_MAX_RECENT_UNKNOWN,
) -> list[str]:
    """
    Canonical markets settled in the last ``recent_days`` with no usable
    row in the store, newest first, capped at ``max_markets``. This is the
    self-heal for a window the step missed and the seed on a first run.
    """
    if (
        df_canonical is None
        or df_canonical.empty
        or "marketId" not in df_canonical.columns
        or "settledDate" not in df_canonical.columns
    ):
        return []
    known: set[str] = set()
    if df_commission is not None and not df_commission.empty:
        amounts = pd.to_numeric(df_commission["commission"], errors="coerce")
        for mid, amount in zip(df_commission["marketId"], amounts):
            if pd.notna(amount):
                known.add(decimal_key(_cell(mid)))

    settled = _parse_utc(df_canonical["settledDate"])
    cutoff = pd.Timestamp(_now_utc(now)) - timedelta(days=recent_days)
    recent = df_canonical.loc[settled >= cutoff, ["marketId"]].copy()
    recent["_settled"] = settled[settled >= cutoff]
    recent["marketId"] = recent["marketId"].map(_cell).str.strip()
    recent = recent[recent["marketId"] != ""]
    if recent.empty:
        return []
    latest = (
        recent.groupby("marketId", sort=False)["_settled"]
        .max()
        .sort_values(ascending=False)
    )
    result: list[str] = []
    seen: set[str] = set()
    for mid in latest.index:
        key = decimal_key(mid)
        if key in known or key in seen:
            continue
        seen.add(key)
        result.append(mid)
        if len(result) >= max_markets:
            break
    return result


def merge_commission(
    df_commission: pd.DataFrame,
    observed: Iterable[MarketCommission],
    *,
    now: Optional[datetime] = None,
) -> pd.DataFrame:
    """
    Upsert fresh grouped rows into the store; the latest read wins because a
    market's grouped totals only grow as its legs settle and its commission
    appears when it closes. Markets match on the numeric key so a damaged
    spelling and a clean one update the same row, keeping the longer id.
    """
    now_str = _fmt_utc(_now_utc(now))
    base = df_commission if df_commission is not None else empty_commission_frame()
    rows: dict[str, dict[str, str]] = {}
    for rec in base.to_dict("records"):
        mid = _cell(rec.get("marketId")).strip()
        if not mid:
            continue
        rows[decimal_key(mid)] = {c: _cell(rec.get(c)) for c in COMMISSION_COLUMNS}

    for obs in observed:
        key = decimal_key(obs.market_id)
        row = rows.get(key) or {c: "" for c in COMMISSION_COLUMNS}
        if len(obs.market_id) > len(row["marketId"]):
            row["marketId"] = obs.market_id
        row["eventTypeId"] = obs.event_type_id
        row["settledDateUtc"] = obs.settled_date_utc
        row["betCount"] = str(obs.bet_count)
        row["grossProfit"] = f"{obs.gross_profit:.2f}"
        row["commission"] = f"{obs.commission:.2f}"
        row["fetchedUtc"] = now_str
        rows[key] = row

    if not rows:
        return empty_commission_frame()
    return pd.DataFrame(list(rows.values()), columns=COMMISSION_COLUMNS)


# -----------------------------
# Orchestration
# -----------------------------


def update_market_commission(
    *,
    client: Any,
    cache_dir: Path,
    from_dt: Optional[datetime],
    to_dt: Optional[datetime],
    df_status: Optional[pd.DataFrame] = None,
    df_canonical: Optional[pd.DataFrame] = None,
    now: Optional[datetime] = None,
    chunk_days: int = 30,
    status_cb: Optional[Callable[[str], None]] = None,
    sleep_seconds: float = 0.2,
    recent_days: int = DEFAULT_RECENT_DAYS,
    max_recent_unknown: int = DEFAULT_MAX_RECENT_UNKNOWN,
) -> CommissionResult:
    """
    Read grouped rows for the window (when one is given), for every
    ever-pending market whose figure is not yet final, and for every recent
    canonical market the store has never seen; merge them into the store
    and write it back. Raises on failure so the runner can report the step
    as a warning; the file is only replaced after a complete fetch.
    """

    def say(msg: str) -> None:
        if status_cb:
            try:
                status_cb(msg)
            except Exception as exc:
                logger.warning("Commission status callback failed: %s", exc)

    path = resolve_commission_path(cache_dir)
    df_commission = load_market_commission(path)

    observed: list[MarketCommission] = []
    if from_dt is not None and to_dt is not None and from_dt < to_dt:
        say(
            f"Commission: reading grouped markets {from_dt:%Y-%m-%d} -> {to_dt:%Y-%m-%d}..."
        )
        observed.extend(
            fetch_commission_for_window(
                client,
                from_dt,
                to_dt,
                chunk_days=chunk_days,
                status_cb=say,
                sleep_seconds=sleep_seconds,
            )
        )
    window_count = len(observed)

    pending = select_requery_markets(df_status, df_commission)
    unseen = select_recent_unknown_markets(
        df_canonical,
        df_commission,
        now=now,
        recent_days=recent_days,
        max_markets=max_recent_unknown,
    )
    # Markets the window read just returned need no second read.
    already = {decimal_key(o.market_id) for o in observed}
    already.update(decimal_key(m) for m in pending)
    requery = pending + [m for m in unseen if decimal_key(m) not in already]
    if requery:
        say(
            f"Commission: re-reading {len(pending):,} ever-pending and "
            f"{len(requery) - len(pending):,} unread recent market(s) by id..."
        )
        observed.extend(
            fetch_commission_for_markets(
                client, requery, now=now, sleep_seconds=sleep_seconds
            )
        )

    if not observed and not requery:
        return CommissionResult(
            attempted=True,
            fetched=0,
            requeried=0,
            message="Commission: nothing to read.",
            path=path,
        )

    merged = merge_commission(df_commission, observed, now=now)
    save_market_commission(merged, path)
    total_commission = sum(o.commission for o in observed)
    msg = (
        f"Commission: read {window_count:,} market(s) in window and "
        f"{len(requery):,} re-queried; {len(merged):,} market(s) on file; "
        f"{total_commission:,.2f} commission in this read."
    )
    say(msg)
    return CommissionResult(
        attempted=True,
        fetched=len(observed),
        requeried=len(requery),
        message=msg,
        path=path,
    )

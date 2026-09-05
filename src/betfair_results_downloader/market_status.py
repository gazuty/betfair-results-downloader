"""
Per-market settlement status: partially settled versus fully settled.

Betfair settles the losing runners of an outright market as they are
eliminated, so a tournament-winner market delivers "settled" bets for days
or weeks while its outcome is still open (the Men's US Open 2026 Winner
market settled 14 lay bets over six timestamps while 22 runners were still
active). ``listClearedOrders`` -- the downloader's source -- carries no
market status, so those rows are indistinguishable from a finished market.

``listMarketBook`` does carry it: ``status`` is OPEN or SUSPENDED while the
market is partially settled and CLOSED once it is fully settled. Two facts
about that endpoint shape everything here, both verified live on
2026-09-06:

- A CLOSED market stays in the book for a variable, undocumented period:
  most racing markets were still returned two weeks after settlement, a
  minority were gone within a day. Retention cannot be relied on, so the
  check runs in every pipeline run, right after the download.
- An OPEN market is always returned. Every id we ask about came from a real
  cleared order, so "absent" is recorded as CLOSED -- provisionally: a
  market closed by absence is asked about again on the following runs for
  :data:`DEFAULT_ABSENT_RECHECK_HOURS`, so one dropped row in a response
  cannot permanently flip a live outright to fully settled.

The observations are persisted in ``<results_csv_dir>/.cache/`` keyed by
marketId (see :data:`STATUS_COLUMNS`), and the daily report holds back the
markets still pending. Markets that were ever seen pending also record when
they were first seen CLOSED, so the report can count the whole market on the
day it actually finished rather than scattering its legs over weeks that have
already been reported.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional

import pandas as pd

from .betfair_net import chunked, retry_betfair_call
from .csv_utils import decimal_key

logger = logging.getLogger(__name__)

STATUS_FILENAME = "market_settlement_status.csv"

STATUS_CLOSED = "CLOSED"

# A market is fully settled only when Betfair says CLOSED; every other status
# (OPEN, SUSPENDED, INACTIVE) means bets may still be outstanding.
SOURCE_BOOK = "book"
SOURCE_ABSENT = "absent"

STATUS_COLUMNS: list[str] = [
    "marketId",
    "status",
    "activeRunners",
    "source",
    "checkedUtc",
    # First time this market was observed in a non-CLOSED state; empty for
    # markets that were already CLOSED the first time we looked (racing,
    # match odds). Set once and never cleared: it is what tells the report
    # that this market's profit belongs to the day it closed.
    "firstPendingUtc",
    # First time this market was observed CLOSED (or absent); empty while
    # pending.
    "closedObservedUtc",
]

# listMarketBook is capped at 200 weight points per request. A bare book
# (no price projection) costs well under 5 points per market, so 40 stays
# under the cap with room to spare whatever the exact per-market weight.
DEFAULT_BATCH_SIZE = 40

# Report windows are at most one week; a fortnight of unknown markets is
# re-checked so a run whose status step failed heals itself on the next one
# and a first run seeds every market the report could show.
DEFAULT_RECENT_DAYS = 14

# Ceiling on the unknown-market seed per run, newest settlements first. The
# live canonical held ~2,000 markets in a fortnight (50 calls, ~30s); the
# cap keeps a pathological first run from stalling the pipeline ahead of
# the Azure publish, and the remainder is picked up next run.
DEFAULT_MAX_RECENT_UNKNOWN = 2_000

# A market closed by absence is asked about again for this long. An open
# market is always returned, so a genuine close stays absent and costs one
# extra id per run for two days; a market that was merely dropped from one
# response comes back OPEN and the merge records it honestly.
DEFAULT_ABSENT_RECHECK_HOURS = 48

# CLOSED observations older than this are pruned. Nothing reads them: the
# report looks back one week, and a market cannot un-close. Pending rows
# are never pruned -- dropping one would silently count a live outright as
# final -- but the run message names the oldest so a stuck row is visible.
DEFAULT_KEEP_CLOSED_DAYS = 90

_ISO_Z = "%Y-%m-%dT%H:%M:%SZ"


def _fmt_utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime(_ISO_Z)


def _now_utc(now: Optional[datetime]) -> datetime:
    if now is None:
        return datetime.now(timezone.utc)
    if now.tzinfo is None:
        return now.replace(tzinfo=timezone.utc)
    return now.astimezone(timezone.utc)


def _parse_utc(series: pd.Series) -> pd.Series:
    return pd.to_datetime(series, utc=True, errors="coerce", format="ISO8601")


def is_pending_status(status: Any) -> bool:
    """True for any status that is not a fully settled market."""
    return str(status).strip().upper() != STATUS_CLOSED


@dataclass(frozen=True)
class BookStatus:
    """One market's status as observed from listMarketBook."""

    status: str
    active_runners: int
    source: str  # SOURCE_BOOK or SOURCE_ABSENT


@dataclass
class MarketStatusResult:
    attempted: bool
    checked: int
    closed: int
    pending: int
    message: str
    path: Optional[Path] = None


# -----------------------------
# Persistence
# -----------------------------


def resolve_status_path(cache_dir: Path) -> Path:
    return Path(cache_dir) / STATUS_FILENAME


def empty_status_frame() -> pd.DataFrame:
    return pd.DataFrame({c: pd.Series(dtype=str) for c in STATUS_COLUMNS})


def load_market_status(path: Path) -> pd.DataFrame:
    """
    Load the status file as strings. A missing file is an empty frame;
    an unreadable one raises so the caller decides -- the runner must not
    overwrite a corrupt file with a fresh one that has forgotten every
    pending market, and the report must not silently count them as final.
    """
    path = Path(path)
    if not path.exists():
        return empty_status_frame()
    # dtype=str for the same reason as the canonical: inferred types would
    # rewrite marketId "1.251500100" as 1.2515001. keep_default_na=False
    # keeps the empty pending/closed timestamps as "" rather than NaN.
    df = pd.read_csv(path, dtype=str, keep_default_na=False)
    for col in STATUS_COLUMNS:
        if col not in df.columns:
            df[col] = ""
    return df[STATUS_COLUMNS]


def save_market_status(df: pd.DataFrame, path: Path) -> None:
    """Write via a temp file and rename so a crash cannot leave a torn file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(path.name + ".tmp")
    try:
        df.reindex(columns=STATUS_COLUMNS).to_csv(tmp_path, index=False)
        tmp_path.replace(path)
    finally:
        if tmp_path.exists():
            tmp_path.unlink()


# -----------------------------
# Betfair: listMarketBook
# -----------------------------


def fetch_market_statuses(
    client: Any,
    market_ids: Iterable[str],
    *,
    batch_size: int = DEFAULT_BATCH_SIZE,
    sleep_seconds: float = 0.2,
    sleep: Callable[[float], Any] = time.sleep,
    max_retries: int = 5,
) -> dict[str, BookStatus]:
    """
    Observe the current status of every id in ``market_ids``.

    Ids absent from Betfair's response are recorded as CLOSED with source
    ``absent``: open markets are always returned, and these ids all came
    from cleared orders, so absence means the market has closed and aged
    out of the book. :func:`select_markets_to_check` keeps asking about
    such markets for a while, so a single dropped row is self-correcting.

    Returns a dict keyed by the *requested* id spelling, so callers can join
    back without worrying about how Betfair rendered the id.
    """
    requested = [str(m).strip() for m in market_ids if str(m).strip()]
    result: dict[str, BookStatus] = {}
    if not requested:
        return result

    for idx, batch in enumerate(chunked(requested, batch_size)):
        if idx:
            sleep(sleep_seconds)
        books = retry_betfair_call(
            lambda batch=batch: client.betting.list_market_book(market_ids=batch),
            max_attempts=max_retries,
        )
        by_key: dict[str, Any] = {}
        for book in books or []:
            by_key[decimal_key(getattr(book, "market_id", ""))] = book
        for mid in batch:
            book = by_key.get(decimal_key(mid))
            if book is None:
                result[mid] = BookStatus(STATUS_CLOSED, 0, SOURCE_ABSENT)
                continue
            status = str(getattr(book, "status", "") or "").upper()
            active = getattr(book, "number_of_active_runners", None)
            try:
                active_int = int(active) if active is not None else 0
            except (TypeError, ValueError):
                active_int = 0
            result[mid] = BookStatus(status, active_int, SOURCE_BOOK)
    return result


# -----------------------------
# Selection and merge
# -----------------------------


def _clean_id(raw: Any) -> str:
    mid = str(raw).strip()
    if not mid or mid.lower() == "nan":
        return ""
    return mid


def select_markets_to_check(
    window_market_ids: Iterable[Any],
    df_status: pd.DataFrame,
    df_canonical: Optional[pd.DataFrame],
    *,
    now: Optional[datetime] = None,
    recent_days: int = DEFAULT_RECENT_DAYS,
    max_recent_unknown: int = DEFAULT_MAX_RECENT_UNKNOWN,
    absent_recheck_hours: int = DEFAULT_ABSENT_RECHECK_HOURS,
) -> list[str]:
    """
    The ids to ask Betfair about this run:

    - everything the window touched;
    - every market still pending in the status file, so a closed outright
      is eventually seen CLOSED;
    - every market closed *by absence* within ``absent_recheck_hours``, so
      one dropped response row cannot permanently mark a live market
      final;
    - up to ``max_recent_unknown`` canonical markets settled within
      ``recent_days`` (newest first) that have no record yet -- the
      self-heal after a failed step, and the first-run seed.

    One id per market: float-damaged spellings collapse on the numeric key
    and the *longest* spelling is the one sent. A damaged id is always a
    truncation of the real nine-digit one, and Betfair cannot resolve the
    truncated form -- asking with it would look exactly like a closed market.
    """
    now_dt = _now_utc(now)
    chosen: dict[str, str] = {}

    def add(raw: Any) -> None:
        mid = _clean_id(raw)
        if not mid:
            return
        key = decimal_key(mid)
        if len(mid) > len(chosen.get(key, "")):
            chosen[key] = mid

    for mid in window_market_ids:
        add(mid)

    known_keys: set[str] = set()
    if df_status is not None and not df_status.empty:
        known_keys = set(df_status["marketId"].map(decimal_key))
        pending = df_status["status"].map(is_pending_status)
        closed_at = _parse_utc(df_status["closedObservedUtc"])
        recheck_cutoff = pd.Timestamp(now_dt) - pd.Timedelta(hours=absent_recheck_hours)
        provisional = (
            ~pending
            & (df_status["source"] == SOURCE_ABSENT)
            & closed_at.notna()
            & (closed_at >= recheck_cutoff)
        )
        for mid in df_status.loc[pending | provisional, "marketId"]:
            add(mid)

    if (
        df_canonical is not None
        and not df_canonical.empty
        and {"marketId", "settledDate"} <= set(df_canonical.columns)
        and max_recent_unknown > 0
    ):
        settled = _parse_utc(df_canonical["settledDate"])
        cutoff = pd.Timestamp(now_dt) - pd.Timedelta(days=recent_days)
        recent = (
            pd.DataFrame({"marketId": df_canonical["marketId"], "settled": settled})
            .loc[settled >= cutoff]
            .sort_values("settled", ascending=False, kind="mergesort")
        )
        added = 0
        for mid in recent["marketId"].drop_duplicates():
            if added >= max_recent_unknown:
                break
            cleaned = _clean_id(mid)
            if not cleaned:
                continue
            key = decimal_key(cleaned)
            if key in known_keys or key in chosen:
                continue
            add(cleaned)
            added += 1

    return list(chosen.values())


def _cell(value: Any) -> str:
    """Honest string form of a status-file cell: None/NaN -> "", 0 -> "0"."""
    if value is None:
        return ""
    if isinstance(value, float) and value != value:  # NaN
        return ""
    return str(value)


def merge_statuses(
    df_status: pd.DataFrame,
    observed: Mapping[str, BookStatus],
    *,
    now: Optional[datetime] = None,
) -> pd.DataFrame:
    """
    Fold fresh observations into the status frame.

    - ``firstPendingUtc`` is set the first time a market is seen not
      CLOSED and never cleared afterwards.
    - ``closedObservedUtc`` is set the first time a market is seen CLOSED.
      Betfair can, rarely, reopen a market to re-settle it -- and a market
      closed by absence can turn out to have been merely dropped from one
      response; a non-CLOSED observation after CLOSED is recorded honestly
      (status updated, closed timestamp cleared) rather than argued with.
    - Markets are matched on the numeric key, so a damaged spelling in the
      file and a clean one from the API update the same row -- and the row
      keeps the longest spelling seen.
    """
    now_str = _fmt_utc(_now_utc(now))
    base = df_status if df_status is not None else empty_status_frame()
    rows: dict[str, dict[str, str]] = {}
    for rec in base.to_dict("records"):
        mid = _clean_id(rec.get("marketId"))
        if not mid:
            continue
        rows[decimal_key(mid)] = {c: _cell(rec.get(c)) for c in STATUS_COLUMNS}

    for raw_mid, obs in observed.items():
        mid = _clean_id(raw_mid)
        if not mid:
            continue
        key = decimal_key(mid)
        row = rows.get(key) or {c: "" for c in STATUS_COLUMNS}
        if len(mid) > len(row["marketId"]):
            # A damaged spelling in the file is upgraded the moment the full
            # one is observed; the file's id is what later runs send, and
            # Betfair cannot resolve the truncated form.
            row["marketId"] = mid
        row["status"] = obs.status
        row["activeRunners"] = str(obs.active_runners)
        row["source"] = obs.source
        row["checkedUtc"] = now_str
        if is_pending_status(obs.status):
            if not row["firstPendingUtc"]:
                row["firstPendingUtc"] = now_str
            row["closedObservedUtc"] = ""
        elif not row["closedObservedUtc"]:
            row["closedObservedUtc"] = now_str
        rows[key] = row

    if not rows:
        return empty_status_frame()
    return pd.DataFrame(list(rows.values()), columns=STATUS_COLUMNS)


def prune_closed(
    df_status: pd.DataFrame,
    *,
    now: Optional[datetime] = None,
    keep_days: int = DEFAULT_KEEP_CLOSED_DAYS,
) -> pd.DataFrame:
    """Drop CLOSED rows whose close was observed more than ``keep_days`` ago."""
    if df_status is None or df_status.empty or keep_days <= 0:
        return df_status
    closed_at = _parse_utc(df_status["closedObservedUtc"])
    cutoff = pd.Timestamp(_now_utc(now)) - timedelta(days=keep_days)
    is_closed = ~df_status["status"].map(is_pending_status)
    stale = is_closed & closed_at.notna() & (closed_at < cutoff)
    return df_status.loc[~stale].reset_index(drop=True)


def _oldest_pending_days(df_status: pd.DataFrame, now: datetime) -> Optional[int]:
    pending = df_status[df_status["status"].map(is_pending_status)]
    if pending.empty:
        return None
    first = _parse_utc(pending["firstPendingUtc"]).dropna()
    if first.empty:
        return None
    return int((pd.Timestamp(now) - first.min()).total_seconds() // 86400)


# -----------------------------
# Orchestration
# -----------------------------


def update_market_status(
    *,
    client: Any,
    cache_dir: Path,
    df_window: Optional[pd.DataFrame],
    df_canonical: Optional[pd.DataFrame],
    now: Optional[datetime] = None,
    status_cb: Optional[Callable[[str], None]] = None,
    batch_size: int = DEFAULT_BATCH_SIZE,
    sleep_seconds: float = 0.2,
    recent_days: int = DEFAULT_RECENT_DAYS,
    max_recent_unknown: int = DEFAULT_MAX_RECENT_UNKNOWN,
    absent_recheck_hours: int = DEFAULT_ABSENT_RECHECK_HOURS,
    keep_closed_days: int = DEFAULT_KEEP_CLOSED_DAYS,
) -> MarketStatusResult:
    """
    Load the status file, check every market that needs it, merge, prune,
    and write the file back. Raises on failure so the runner can report the
    step as a warning; the file is only replaced after a complete fetch.
    """

    def say(msg: str) -> None:
        if status_cb:
            try:
                status_cb(msg)
            except Exception:
                pass

    path = resolve_status_path(cache_dir)
    df_status = load_market_status(path)

    window_ids: list[Any] = []
    if (
        df_window is not None
        and not df_window.empty
        and "marketId" in df_window.columns
    ):
        window_ids = df_window["marketId"].dropna().astype(str).unique().tolist()

    to_check = select_markets_to_check(
        window_ids,
        df_status,
        df_canonical,
        now=now,
        recent_days=recent_days,
        max_recent_unknown=max_recent_unknown,
        absent_recheck_hours=absent_recheck_hours,
    )
    if not to_check:
        return MarketStatusResult(
            attempted=True,
            checked=0,
            closed=0,
            pending=0,
            message="Market status: nothing to check.",
            path=path,
        )

    say(f"Market status: checking {len(to_check):,} market(s) via listMarketBook...")
    observed = fetch_market_statuses(
        client, to_check, batch_size=batch_size, sleep_seconds=sleep_seconds
    )
    merged = merge_statuses(df_status, observed, now=now)
    merged = prune_closed(merged, now=now, keep_days=keep_closed_days)
    save_market_status(merged, path)

    pending_now = int(merged["status"].map(is_pending_status).sum())
    closed_seen = sum(1 for o in observed.values() if not is_pending_status(o.status))
    absent = sum(1 for o in observed.values() if o.source == SOURCE_ABSENT)
    msg = (
        f"Market status: checked {len(observed):,} market(s); "
        f"{closed_seen:,} closed ({absent:,} by absence), "
        f"{len(observed) - closed_seen:,} still pending; "
        f"{pending_now:,} pending in total."
    )
    oldest = _oldest_pending_days(merged, _now_utc(now))
    if oldest is not None:
        msg += f" Oldest pending market first seen {oldest:,} day(s) ago."
    say(msg)
    return MarketStatusResult(
        attempted=True,
        checked=len(observed),
        closed=closed_seen,
        pending=pending_now,
        message=msg,
        path=path,
    )

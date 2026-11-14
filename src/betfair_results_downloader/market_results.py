"""
The market results feed: one row per finished market, net of commission.

This is the contract the Betfair dashboard ingests in place of the website
"Betting P&L" download. It is written to ``<results_csv_dir>/`` as two
files:

- ``market_results.csv`` -- one row per market, columns
  :data:`FEED_COLUMNS`. Settled times are given in UTC and, to match the
  website download the dashboard's history came from, as naive Sydney time.
- ``market_results.meta.json`` -- when the feed was built, a SHA-256 of
  the CSV, and what was held back. A reader checks the hash so it never
  pairs a new CSV with an old summary, and can skip a publish when the
  hash is unchanged.

The feed is cumulative. A market whose bets have moved out of the rolling
canonical into the yearly archives keeps the row it last had, carried
forward from the previous feed, so history never drops out of the file.

A market appears only when its figure is final, decided by the same code
as the daily DM report so the two never disagree:

- A partially settled market (an outright still being settled leg by leg)
  is held back until Betfair closes it. It then appears once, for the whole
  market, at the moment the close was observed (see
  :func:`~.reporting.daily_dm_report.apply_settlement_status`). The website
  download instead caught the interim legs in one week and the full market
  again in a later one, which double-counted it.
- A market whose commission is not yet known is held back until a later
  run reads it (see :func:`~.reporting.daily_dm_report.market_commission_frame`).
  Publishing its gross would overstate it, and publishing gross less zero
  would be a silent zero commission.

The meta records both counts, and the oldest commission-unknown market's
age, so the runner can warn when a market is stuck rather than late.
"""

from __future__ import annotations

import hashlib
import json
from io import BytesIO
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

import pandas as pd

from .commission import (
    DEFAULT_REQUERY_LOOKBACK_DAYS,
    load_market_commission,
    resolve_commission_path,
)
from .csv_utils import decimal_key
from .downloader_core import resolve_enrichment_cache_dir
from .market_names import (
    SOURCE_FALLBACK,
    UNKNOWN_COUNTRY,
    resolve_market_names,
)
from .market_status import (
    CANONICAL_FILENAME,
    DEFAULT_ABSENT_RECHECK_HOURS,
    RACING_EVENT_TYPE_IDS,
    SOURCE_ABSENT,
    _fmt_utc,
    _now_utc,
    load_market_status,
    resolve_status_path,
)
from .racecard import load_racecard, resolve_racecard_path
from .reporting.daily_dm_report import (
    apply_settlement_status,
    market_commission_frame,
    supplement_with_archived_legs,
)
from .reporting.schema import SYDNEY_TZ, normalize_cleared_orders_schema

logger = logging.getLogger(__name__)

FEED_FILENAME = "market_results.csv"
META_FILENAME = "market_results.meta.json"
# The last consistent generation, kept so an interrupted write can heal.
PREV_FEED_FILENAME = "market_results.prev.csv"
PREV_META_FILENAME = "market_results.prev.meta.json"

# Bumped whenever a column is added, removed or changes meaning; the
# dashboard refuses a version it does not know.
SCHEMA_VERSION = 1

FEED_COLUMNS: list[str] = [
    "marketId",
    "eventTypeId",
    "sport",
    # "Horse Racing / Morphettville (AUS) 14th Nov : R7 1200m Hcp"
    "market",
    # racecard | catalogue | item_description | fallback (see market_names)
    "nameSource",
    # The market's last settled leg, or the observed close of a market that
    # was ever partially settled.
    "settledUtc",
    "settledLocal",
    "betCount",
    "grossProfit",
    "commission",
    "netProfit",
]

# The canonical's columns the feed needs. betId lets the archived legs of a
# long-running outright be merged without double counting.
_CANONICAL_COLUMNS = [
    "betId",
    "marketId",
    "eventTypeId",
    "settledDate",
    "profit",
    "evt_eventName",
    "mkt_marketName",
]

# A commission figure normally lands within a run or two of settlement. One
# still missing after this long is stuck, not late, and is worth a warning.
STUCK_COMMISSION_HOURS = 48

# A market must have a settlement-status row before it is published, unless
# it is a racing market settled more than this many days ago. The report's
# rule that "no row means final" would otherwise publish an outright that
# is still open as finished. The status step records every market a run
# touches, seeds a fortnight of unrecorded ones, and seeds every unrecorded
# non-racing market of any age (market_status). So a missing row means that
# step failed or has not reached the market yet. Racing markets close
# within hours, so an old unrecorded race predates status tracking and is
# final.
STATUS_REQUIRED_DAYS = 14

# How long after an observed close a zero commission must still read zero
# before the feed accepts it as final (see _confirmed_zero_commission_keys).
CONFIRMED_ZERO_AFTER = timedelta(hours=24)


@dataclass
class FeedResult:
    feed: pd.DataFrame
    meta: dict[str, Any]
    message: str
    warnings: list[str] = field(default_factory=list)


def _market_keys(final: pd.DataFrame) -> list[str]:
    """
    The per-market key :func:`market_commission_frame` uses, row for row:
    the numeric marketId, or ``blank:<position>`` for a row without one so
    that each such row stands as its own commission-unknown market.
    """
    ids = final["marketId"].fillna("").astype(str).str.strip()
    return [
        decimal_key(mid) if mid else f"blank:{pos}"
        for pos, mid in enumerate(ids.tolist())
    ]


def _store_by_key(market_commission: Optional[pd.DataFrame]) -> pd.DataFrame:
    """
    The commission store indexed by numeric key: its marketId spelling,
    Betfair's whole-market gross and its bet count.

    The spelling matters because the dashboard keys on the id: the store's
    ids come straight from the API as strings, while the canonical still
    holds float-damaged spellings for some historical rows ("1.25194156"
    for "1.251941560").

    The gross matters because it is Betfair's own figure for the market,
    and it is the one the website download agreed with to the cent. The
    canonical's per-bet sum can differ from it: a long-running outright's
    early bets can be older than the canonical's start, and a bet Betfair
    settled and later took back out of the market stays in the canonical
    under its own betId. Checked live on 2026-11-14: 4 of 40,206 markets
    differed, and in each one the store matched the website.
    """
    columns = ["storeMarketId", "storeGross", "storeBetCount"]
    if market_commission is None or market_commission.empty:
        return pd.DataFrame(columns=columns)
    store = market_commission.copy()
    store["storeMarketId"] = store["marketId"].fillna("").astype(str).str.strip()
    store = store[store["storeMarketId"] != ""]
    store["_key"] = store["storeMarketId"].map(decimal_key)
    store["storeGross"] = pd.to_numeric(store["grossProfit"], errors="coerce")
    store["storeBetCount"] = pd.to_numeric(store["betCount"], errors="coerce")
    store = store.drop_duplicates(subset=["_key"], keep="last")
    return store.set_index("_key")[columns]


def _confirmed_zero_commission_keys(
    market_commission: Optional[pd.DataFrame],
    market_status: Optional[pd.DataFrame],
    rows_latest: pd.Series,
) -> set[str]:
    """
    Markets whose zero commission is Betfair's final answer, not the
    pre-close placeholder :func:`market_commission_frame` takes it for.

    The placeholder rule treats a zero against a win above 15 cents as
    unknown, because a partially settled market reports 0.0 until it
    closes. That is right for a read taken before the close, but Betfair
    rounds commission to the nearest cent, so a win at a rate below any
    seen so far can still genuinely be charged nothing. Market 1.264531699
    ($0.11 won, still $0.00 thirteen days after it closed) showed the
    threshold was once too low. This is the safety net for the next such
    case: without it the market would be held back, and alerted on,
    forever.

    A zero is accepted only when all of these hold: the market was never
    seen partially settled, the status step saw it CLOSED, the store row
    was read at least :data:`CONFIRMED_ZERO_AFTER` after that, and the
    store row covers the canonical's latest leg. ``rows_latest`` maps the
    numeric key to that latest leg (tz-aware).
    """
    if (
        market_commission is None
        or market_commission.empty
        or market_status is None
        or market_status.empty
    ):
        return set()
    store = market_commission.copy()
    store["_key"] = store["marketId"].fillna("").astype(str).map(decimal_key)
    store = store[store["_key"] != ""].drop_duplicates(subset=["_key"], keep="last")
    amount = pd.to_numeric(store["commission"], errors="coerce")
    store = store.loc[amount == 0.0]
    if store.empty:
        return set()
    status = market_status.copy()
    status["_key"] = status["marketId"].fillna("").astype(str).map(decimal_key)
    status = status[status["_key"] != ""].drop_duplicates(subset=["_key"], keep="last")
    status = status.loc[
        (status["status"].astype(str).str.upper() == "CLOSED")
        & (status["firstPendingUtc"].fillna("").astype(str) == "")
    ].set_index("_key")
    closed_at = pd.to_datetime(
        status["closedObservedUtc"], utc=True, errors="coerce", format="ISO8601"
    )
    joined = store.join(closed_at.rename("closed"), on="_key", how="inner")
    fetched = pd.to_datetime(
        joined["fetchedUtc"], utc=True, errors="coerce", format="ISO8601"
    )
    settled = pd.to_datetime(
        joined["settledDateUtc"], utc=True, errors="coerce", format="ISO8601"
    )
    # A plain dict: mapping onto a datetime Series with no matching keys
    # makes pandas try to cast the result to float and raise.
    latest_by_key = {str(k): v for k, v in rows_latest.items()}
    latest = pd.to_datetime(
        joined["_key"].map(lambda k: latest_by_key.get(k, pd.NaT)), utc=True
    )
    ok = (
        joined["closed"].notna()
        & fetched.notna()
        & (fetched >= joined["closed"] + CONFIRMED_ZERO_AFTER)
        & settled.notna()
        & latest.notna()
        & (settled >= latest)
    )
    return set(joined.loc[ok, "_key"])


def _hold_unchecked(
    final: pd.DataFrame,
    market_status: Optional[pd.DataFrame],
    now_utc: datetime,
) -> tuple[pd.DataFrame, int]:
    """Drop markets that must have a status row and don't; return the count."""
    if final.empty:
        return final, 0
    recorded: set[str] = set()
    if market_status is not None and not market_status.empty:
        recorded = set(
            market_status["marketId"].fillna("").astype(str).map(decimal_key)
        )
    keys = final["marketId"].fillna("").astype(str).map(decimal_key)
    latest = final.groupby(keys)["settled_dt_utc"].transform("max")
    cutoff = pd.Timestamp(now_utc) - pd.Timedelta(days=STATUS_REQUIRED_DAYS)
    racing = (
        final["eventTypeId"]
        .fillna("")
        .astype(str)
        .str.strip()
        .isin(RACING_EVENT_TYPE_IDS)
    )
    from_archive = (
        final[ARCHIVED_COLUMN].fillna("").astype(str) == "1"
        if ARCHIVED_COLUMN in final.columns
        else pd.Series(False, index=final.index)
    )
    archive_only = from_archive.groupby(keys).transform("all")
    needs_row = (~racing | (latest >= cutoff)) & ~archive_only
    unchecked = (keys != "") & ~keys.isin(recorded) & needs_row
    provisional = keys.isin(_provisional_absent_keys(market_status, now_utc))
    # A non-racing market closed only by absence is not yet confirmed: one
    # dropped response row records an OPEN outright that way, and the next
    # runs re-check it (market_status DEFAULT_ABSENT_RECHECK_HOURS). Held if
    # it could plausibly still be open: seen pending before, or settled a
    # leg recently. A long-finished market absent from the book on its
    # first check is closed in fact, and is published.
    was_pending = keys.isin(_ever_pending_keys(market_status))
    unconfirmed = (
        provisional & ~racing & (was_pending | (latest >= cutoff)) & ~archive_only
    )
    held = unchecked | unconfirmed
    return final.loc[~held], int(keys[held].nunique())


def _ever_pending_keys(market_status: Optional[pd.DataFrame]) -> set[str]:
    if market_status is None or market_status.empty:
        return set()
    first = market_status["firstPendingUtc"].fillna("").astype(str)
    ids = market_status.loc[first != "", "marketId"].fillna("").astype(str)
    return set(ids.map(decimal_key))


def _provisional_absent_keys(
    market_status: Optional[pd.DataFrame], now_utc: datetime
) -> set[str]:
    """Markets recorded CLOSED by absence within the re-check window."""
    if market_status is None or market_status.empty:
        return set()
    closed_at = pd.to_datetime(
        market_status["closedObservedUtc"], utc=True, errors="coerce", format="ISO8601"
    )
    window = pd.Timestamp(now_utc) - pd.Timedelta(hours=DEFAULT_ABSENT_RECHECK_HOURS)
    mask = (
        (market_status["source"].astype(str) == SOURCE_ABSENT)
        & (market_status["status"].astype(str).str.upper() == "CLOSED")
        & closed_at.notna()
        & (closed_at >= window)
    )
    return set(
        market_status.loc[mask, "marketId"].fillna("").astype(str).map(decimal_key)
    )


def _carry_forward(
    feed: pd.DataFrame,
    previous_feed: Optional[pd.DataFrame],
    canonical_keys: set[str],
) -> tuple[pd.DataFrame, int]:
    """Append the previous feed's rows for markets no longer in the canonical."""
    if previous_feed is None or previous_feed.empty:
        return feed, 0
    prev = previous_feed.reindex(columns=FEED_COLUMNS).copy()
    keys = prev["marketId"].fillna("").astype(str).map(decimal_key)
    keep = prev.loc[(keys != "") & ~keys.isin(canonical_keys)].copy()
    if keep.empty:
        return feed, 0
    for col in ("grossProfit", "commission", "netProfit"):
        keep[col] = pd.to_numeric(keep[col], errors="raise").round(2)
    keep["betCount"] = pd.to_numeric(keep["betCount"], errors="raise").astype(int)
    merged = pd.concat([keep, feed], ignore_index=True) if len(feed) else keep
    merged = merged.sort_values(["settledUtc", "marketId"]).reset_index(drop=True)
    return merged.reindex(columns=FEED_COLUMNS), len(keep)


def _read_consistent_pair(
    feed_path: Path, meta_path: Path
) -> Optional[tuple[bytes, dict]]:
    """The pair's bytes and meta when both exist, parse and agree; else None."""
    if not feed_path.exists() or not meta_path.exists():
        return None
    try:
        payload = feed_path.read_bytes()
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if hashlib.sha256(payload).hexdigest() != meta.get("csvSha256"):
        return None
    return payload, meta


def load_previous_feed(results_dir: Path) -> Optional[pd.DataFrame]:
    """
    The feed last written, for carrying aged-out markets forward.

    ``None`` when no feed has been written yet. If the current pair does not
    agree (a run died between its two replacements), the previous
    generation that :func:`write_market_results` keeps is used instead, so
    an interrupted write heals on the next run. Raises when neither pair is
    usable, or the feed is of another schema: rebuilding without it would
    silently drop every market that has aged out of the canonical.
    """
    results_dir = Path(results_dir)
    current = (results_dir / FEED_FILENAME, results_dir / META_FILENAME)
    previous = (results_dir / PREV_FEED_FILENAME, results_dir / PREV_META_FILENAME)
    if not any(path.exists() for path in (*current, *previous)):
        return None
    pair = _read_consistent_pair(*current)
    if pair is None:
        pair = _read_consistent_pair(*previous)
        if pair is None:
            raise ValueError(
                "Previous feed does not match its meta and no consistent earlier "
                "generation is kept; not carrying it forward."
            )
        logger.warning(
            "Results feed and its meta disagree (an interrupted write); "
            "rebuilding from the previous generation."
        )
    payload, meta = pair
    if meta.get("schemaVersion") != SCHEMA_VERSION:
        raise ValueError(
            f"Previous feed is schema {meta.get('schemaVersion')!r}, expected "
            f"{SCHEMA_VERSION}; not carrying it forward."
        )
    return pd.read_csv(BytesIO(payload), dtype=str, keep_default_na=False)


def _first_present(series: pd.Series) -> Any:
    present = series.dropna()
    present = present[present.astype(str).str.strip() != ""]
    return present.iloc[0] if not present.empty else None


def _finish_ready_markets(
    ready: pd.DataFrame,
    market_commission: Optional[pd.DataFrame],
    *,
    racecard: Optional[pd.DataFrame],
    catalogue: Optional[pd.DataFrame],
) -> tuple[pd.DataFrame, int]:
    """
    Turn the markets whose figure is final into feed rows.

    Returns ``(feed, markets whose canonical sum differed from Betfair's
    gross)``. An empty input is an empty feed with the full column set, so
    a run that holds every market back still writes a well-formed file.
    """
    if ready.empty:
        return pd.DataFrame(columns=FEED_COLUMNS), 0
    # A market whose commission is known has a store row by construction;
    # its gross and bet count come from there (see _store_by_key).
    ready = ready.join(_store_by_key(market_commission), on="_key")
    canonical_gross = ready["grossProfit"].astype(float).round(2)
    has_store = ready["storeGross"].notna()
    gross_mismatch = has_store & (
        (ready["storeGross"].round(2) - canonical_gross).abs() > 0.005
    )
    ready.loc[has_store, "marketId"] = ready.loc[has_store, "storeMarketId"]
    ready.loc[has_store, "grossProfit"] = ready.loc[has_store, "storeGross"]
    with_count = has_store & ready["storeBetCount"].notna()
    ready.loc[with_count, "betCount"] = ready.loc[with_count, "storeBetCount"]
    ready["betCount"] = ready["betCount"].astype(int)
    ready = resolve_market_names(ready, racecard=racecard, catalogue=catalogue)

    settled_local = pd.to_datetime(ready["settled_dt_local"]).dt.tz_convert(SYDNEY_TZ)
    ready["settledUtc"] = settled_local.dt.tz_convert("UTC").map(_fmt_utc)
    ready["settledLocal"] = settled_local.dt.strftime("%Y-%m-%d %H:%M:%S")
    ready["grossProfit"] = ready["grossProfit"].astype(float).round(2)
    ready["commission"] = ready["commission"].astype(float).round(2)
    ready["netProfit"] = (ready["grossProfit"] - ready["commission"]).round(2)
    feed = (
        ready.sort_values(["settledUtc", "marketId"])
        .reindex(columns=FEED_COLUMNS)
        .reset_index(drop=True)
    )
    return feed, int(gross_mismatch.sum())


def build_market_results(
    df_raw: pd.DataFrame,
    *,
    market_status: Optional[pd.DataFrame],
    market_commission: Optional[pd.DataFrame],
    racecard: Optional[pd.DataFrame],
    catalogue: Optional[pd.DataFrame],
    previous_feed: Optional[pd.DataFrame] = None,
    now: Optional[datetime] = None,
) -> FeedResult:
    """
    Build the feed and its meta from frames already in memory.

    ``previous_feed`` is the last feed written (strings, as read from disk).
    Its rows for markets the canonical no longer holds at all -- their bets
    have moved to the yearly archives -- are carried forward unchanged, so
    the feed is cumulative without re-reading the archives on every run. A
    market still in the canonical is always rebuilt from it, so a carried
    row can never shadow a newer figure.
    """
    now_utc = _now_utc(now)
    normalized = normalize_cleared_orders_schema(df_raw)
    canonical_keys = set(normalized["marketId"].fillna("").astype(str).map(decimal_key))
    settled = normalized.dropna(subset=["settled_dt_local"]).copy()
    data_through = settled["settled_dt_utc"].max() if not settled.empty else None

    final, pending_rows = apply_settlement_status(settled, market_status)
    final, unchecked_markets = _hold_unchecked(final, market_status, now_utc)
    final = final.reset_index(drop=True)
    commission = market_commission_frame(final, market_commission, market_status)

    final["_key"] = _market_keys(final)
    per_market = (
        final.groupby("_key", sort=False)
        .agg(
            marketId=("marketId", _first_present),
            eventTypeId=("eventTypeId", _first_present),
            evt_eventName=("evt_eventName", _first_present),
            mkt_marketName=("mkt_marketName", _first_present),
            grossProfit=("profit", "sum"),
            betCount=("profit", "size"),
        )
        .reset_index()
        # Keys are strings on both sides; an empty frame would otherwise
        # infer a numeric dtype and refuse the merge.
        .astype({"_key": str})
        .merge(commission.astype({"_key": str}), on="_key", how="left")
    )
    per_market["settled_dt_local"] = per_market["last_settled_local"]

    latest_leg = final.groupby("_key")["settled_dt_local"].max()
    confirmed_zero = _confirmed_zero_commission_keys(
        market_commission, market_status, latest_leg
    )
    flip = per_market["unknown"].fillna(True).astype(bool) & per_market["_key"].isin(
        confirmed_zero
    )
    per_market.loc[flip, ["unknown", "commission"]] = [False, 0.0]

    blank_id = per_market["_key"].astype(str).str.startswith("blank:")
    unknown = per_market["unknown"].fillna(True).astype(bool)
    held = per_market.loc[unknown]
    ready = per_market.loc[~unknown].copy()

    feed, gross_mismatch_markets = _finish_ready_markets(
        ready, market_commission, racecard=racecard, catalogue=catalogue
    )
    feed, carried = _carry_forward(feed, previous_feed, canonical_keys)

    # Stuck commission: rows with no marketId can never be read, so they are
    # counted separately rather than alerting on every run forever.
    held_with_id = held.loc[~held["_key"].astype(str).str.startswith("blank:")]
    # Betfair serves grouped (commission) rows for about a year, so a market
    # older than that -- an archive or an old backfill -- can never learn
    # its commission. It is counted, not alerted on every run.
    reach = pd.Timestamp(now_utc) - pd.Timedelta(days=DEFAULT_REQUERY_LOOKBACK_DAYS)
    unreachable = pd.to_datetime(held_with_id["last_settled_local"], utc=True) < reach
    unreachable_markets = int(unreachable.sum())
    held_with_id = held_with_id.loc[~unreachable]
    oldest_unknown_hours: Optional[float] = None
    if not held_with_id.empty:
        oldest = pd.to_datetime(held_with_id["last_settled_local"]).min()
        oldest_unknown_hours = round(
            (now_utc - oldest.tz_convert("UTC").to_pydatetime()).total_seconds()
            / 3600.0,
            1,
        )
    pending_markets = (
        int(pending_rows["marketId"].map(decimal_key).nunique())
        if not pending_rows.empty
        else 0
    )
    fallback = feed["nameSource"] == SOURCE_FALLBACK
    unknown_country = fallback & feed["market"].astype(str).str.contains(
        f"({UNKNOWN_COUNTRY})", regex=False
    )
    meta: dict[str, Any] = {
        "schemaVersion": SCHEMA_VERSION,
        "generatedUtc": _fmt_utc(now_utc),
        "dataThroughUtc": _fmt_utc(data_through.to_pydatetime())
        if data_through is not None and pd.notna(data_through)
        else None,
        "markets": int(len(feed)),
        "settledFromLocal": feed["settledLocal"].min() if len(feed) else None,
        "settledToLocal": feed["settledLocal"].max() if len(feed) else None,
        "heldPendingMarkets": pending_markets,
        # No status row yet, or closed only by absence and not yet confirmed.
        "heldStatusUncheckedMarkets": unchecked_markets,
        # Markets aged out of the canonical, kept from the previous feed.
        "carriedForwardMarkets": carried,
        "heldCommissionUnknownMarkets": int(len(held_with_id)),
        "oldestCommissionUnknownHours": oldest_unknown_hours,
        "heldNoMarketIdRows": int(blank_id.sum()),
        "heldCommissionUnreachableMarkets": unreachable_markets,
        "nameSources": {
            k: int(v) for k, v in feed["nameSource"].value_counts().items()
        },
        "unknownCountryMarkets": int(unknown_country.sum()),
        # Markets whose canonical per-bet sum differs from Betfair's
        # whole-market gross, which the feed uses. Diagnostic only.
        "canonicalGrossMismatchMarkets": gross_mismatch_markets,
    }

    warnings: list[str] = []
    if (
        oldest_unknown_hours is not None
        and oldest_unknown_hours > STUCK_COMMISSION_HOURS
    ):
        warnings.append(
            f"⚠️ Results feed: {len(held_with_id):,} market(s) held back for "
            f"unknown commission, the oldest settled {oldest_unknown_hours:.0f}h "
            f"ago; they stay off the dashboard until a run reads their commission."
        )
    message = (
        f"Results feed: {len(feed):,} markets; held back "
        f"{pending_markets:,} partially settled, {unchecked_markets:,} with no "
        f"settlement status yet and {len(held_with_id):,} commission-unknown; "
        f"names {meta['nameSources']}."
    )
    return FeedResult(feed=feed, meta=meta, message=message, warnings=warnings)


def _load_canonical(results_dir: Path) -> pd.DataFrame:
    path = Path(results_dir) / CANONICAL_FILENAME
    if not path.exists():
        raise FileNotFoundError(f"No canonical at {path}")
    return pd.read_csv(
        path,
        dtype=str,
        keep_default_na=False,
        usecols=lambda c: c in _CANONICAL_COLUMNS,
    )


def _load_catalogue_cache(cache_dir: Path) -> Optional[pd.DataFrame]:
    """The enrichment step's catalogue cache; ``None`` when absent or damaged."""
    path = Path(cache_dir) / "market_catalogue_event_cache.csv"
    if not path.exists():
        return None
    try:
        return pd.read_csv(path, dtype=str, keep_default_na=False)
    except (OSError, ValueError) as exc:
        logger.warning(
            "Could not read %s (%s: %s); racing names fall back without it.",
            path.name,
            type(exc).__name__,
            exc,
        )
        return None


ARCHIVE_GLOB = "cleared_orders_archive_*.csv.gz"

# Marks rows read from the yearly archives for a market the feed never had.
# Every leg of such a market settled more than the archive window ago (a
# year by default), so it is final whether or not a status row survives:
# CLOSED rows are pruned after 400 days, and history from before status
# tracking never had one.
ARCHIVED_COLUMN = "_fromArchive"


def archive_signature(results_dir: Path) -> list[list[Any]]:
    """Name, size and modification time of every yearly archive, sorted."""
    return [
        [path.name, path.stat().st_size, path.stat().st_mtime_ns]
        for path in sorted(Path(results_dir).glob(ARCHIVE_GLOB))
    ]


def _previous_archive_signature(results_dir: Path) -> Optional[list[list[Any]]]:
    """The archive signature the last feed was built with; None if unknown."""
    for name in (META_FILENAME, PREV_META_FILENAME):
        path = Path(results_dir) / name
        try:
            meta = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if "archiveSignature" in meta:
            return meta["archiveSignature"]
    return None


def _load_archives(results_dir: Path) -> pd.DataFrame:
    """
    Every archived bet, the columns the feed needs only.

    Raises on an unreadable archive: the new signature is recorded once the
    feed is written, so a skipped archive would never be read again.
    """
    frames = [
        pd.read_csv(
            path,
            dtype=str,
            keep_default_na=False,
            usecols=lambda c: c in _CANONICAL_COLUMNS,
        )
        for path in sorted(Path(results_dir).glob(ARCHIVE_GLOB))
    ]
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def build_market_results_from_disk(
    results_dir: Path, *, now: Optional[datetime] = None
) -> FeedResult:
    """
    Build the feed from the files a run leaves in ``results_dir``.

    The previous feed is read to carry forward markets that have aged out
    of the canonical; a damaged one raises rather than drop them. An
    unreadable status or commission file raises: without the status a
    pending outright would be published as final, and without commission
    every market would be held back. The previous feed then stays in place
    and ages, which the dashboard notices. A damaged racecard only costs
    names, so it is read leniently and reported.
    """
    results_dir = Path(results_dir)
    cache_dir = resolve_enrichment_cache_dir(results_dir)
    market_status = load_market_status(resolve_status_path(cache_dir))
    market_commission = load_market_commission(resolve_commission_path(cache_dir))
    warnings: list[str] = []
    try:
        racecard = load_racecard(resolve_racecard_path(cache_dir))
    except (OSError, ValueError) as exc:
        racecard = None
        warnings.append(
            f"⚠️ Results feed: racecard unreadable ({type(exc).__name__}: {exc}); "
            f"racing markets were named from the fallback."
        )
    df_raw = _load_canonical(results_dir)
    df_raw = supplement_with_archived_legs(df_raw, market_status, results_dir)
    previous_feed = load_previous_feed(results_dir)
    # Archived markets reach the feed once, when the archives change: the
    # yearly roll moves bets out of the canonical, and a backfill of old
    # dates writes ordinary markets straight into an archive, so neither
    # would otherwise ever have a feed row to carry forward. Only markets
    # the feed has never had are added. A market already in it keeps its
    # carried row, which was built while its bets and status were current.
    # Unchanged archives are not read again.
    signature = archive_signature(results_dir)
    if signature and signature != _previous_archive_signature(results_dir):
        from .downloader_core import _rows_not_already_held  # noqa: PLC0415

        archived = _load_archives(results_dir)
        if not archived.empty:
            known = set(df_raw["marketId"].fillna("").astype(str).map(decimal_key))
            if previous_feed is not None and not previous_feed.empty:
                known |= set(previous_feed["marketId"].astype(str).map(decimal_key))
            keys = archived["marketId"].fillna("").astype(str).map(decimal_key)
            new_markets = archived.loc[(keys != "") & ~keys.isin(known)]
            extra = _rows_not_already_held(df_raw, new_markets).assign(
                **{ARCHIVED_COLUMN: "1"}
            )
            if len(extra):
                df_raw = pd.concat([df_raw, extra], ignore_index=True)
            logger.info(
                "Results feed: read the yearly archives; %d new archived market(s).",
                extra["marketId"].nunique() if len(extra) else 0,
            )
    result = build_market_results(
        df_raw,
        market_status=market_status,
        market_commission=market_commission,
        racecard=racecard,
        catalogue=_load_catalogue_cache(cache_dir),
        previous_feed=previous_feed,
        now=now,
    )
    result.meta["archiveSignature"] = signature
    result.warnings[:0] = warnings
    return result


def _replace_atomically(path: Path, payload: bytes) -> None:
    tmp_path = path.with_name(path.name + ".tmp")
    try:
        tmp_path.write_bytes(payload)
        tmp_path.replace(path)
    finally:
        if tmp_path.exists():
            tmp_path.unlink()


def write_market_results(results_dir: Path, result: FeedResult) -> Path:
    """
    Write the CSV, then the meta that names its hash.

    Each file is replaced atomically. A reader that lands between the two
    replacements sees a hash mismatch and waits for the next run, rather
    than publishing a new CSV against an old summary.

    Before replacing them, a consistent current pair is copied to
    ``market_results.prev.*``. If the process dies between the two
    replacements, the next run rebuilds from that generation (see
    :func:`load_previous_feed`) and rewrites both, so the mismatch heals.
    """
    results_dir = Path(results_dir)
    results_dir.mkdir(parents=True, exist_ok=True)
    feed_path = results_dir / FEED_FILENAME
    meta_path = results_dir / META_FILENAME
    current = _read_consistent_pair(feed_path, meta_path)
    if current is not None:
        payload, meta_now = current
        _replace_atomically(results_dir / PREV_FEED_FILENAME, payload)
        _replace_atomically(
            results_dir / PREV_META_FILENAME,
            (json.dumps(meta_now, indent=2, sort_keys=True) + "\n").encode("utf-8"),
        )
    csv_bytes = result.feed.to_csv(index=False).encode("utf-8")
    meta = dict(result.meta, csvSha256=hashlib.sha256(csv_bytes).hexdigest())
    _replace_atomically(feed_path, csv_bytes)
    _replace_atomically(
        meta_path,
        (json.dumps(meta, indent=2, sort_keys=True) + "\n").encode("utf-8"),
    )
    return feed_path

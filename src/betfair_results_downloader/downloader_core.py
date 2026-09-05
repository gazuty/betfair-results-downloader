from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Callable, List, Optional, Tuple, Union

import gzip
import json
import logging
import re
import shutil
import time
from zoneinfo import ZoneInfo

import pandas as pd

import betfairlightweight
from betfairlightweight import filters
from betfairlightweight.exceptions import APIError  # noqa: F401 (kept for callers/tests)

from .betfair_net import chunked as _chunked, retry_betfair_call
from .config import EVENTTYPE_GREYHOUNDS, EVENTTYPE_HORSES
from .csv_utils import (
    betid_keys,
    clean_and_remove_duplicates,
    decimal_key,
    update_csv_with_new_data,
)
from .scheduler.date_windows import chunk_date_range

logger = logging.getLogger(__name__)


DateLike = Union[date, datetime]


# -----------------------------
# Cache directory resolution
# -----------------------------


def resolve_enrichment_cache_dir(results_csv_dir: Path) -> Path:
    """
    Resolve enrichment cache directory under the dataset folder.

    Returns: <results_csv_dir>/.cache
    """
    return results_csv_dir / ".cache"


# -----------------------------
# Small results containers
# -----------------------------


@dataclass
class DownloadResult:
    attempted: bool
    rows_downloaded: int
    message: str
    df_co: Optional[pd.DataFrame] = None
    from_utc: Optional[datetime] = None
    to_utc: Optional[datetime] = None


@dataclass
class EnrichResult:
    attempted: bool
    markets_requested: int
    markets_returned: int
    message: str

    # Additional, backward-compatible enrichment stats (optional)
    unique_market_ids: int = 0
    use_cache: bool = True
    cache_path: Optional[str] = None
    cache_snapshot_path: Optional[str] = None
    cache_rows: int = 0
    cache_hits: int = 0
    cache_misses: int = 0
    batch_size: int = 0


@dataclass
class CsvWriteResult:
    canonical_path: Path
    snapshot_path: Path
    rows_in_canonical: int
    message: str
    # The full post-write canonical frame, held so Azure aggregation can
    # cover split settlements without re-reading the 270MB file from disk.
    df_canonical: Optional[pd.DataFrame] = None


@dataclass
class AzurePrepResult:
    attempted: bool
    rows_after_filter: int
    markets_aggregated: int
    message: str
    df_market_results: Optional[pd.DataFrame] = None
    rows_to_write: Optional[List[Tuple[Decimal, Decimal, str]]] = None


# -----------------------------
# Betfair: cleared orders
# -----------------------------


def _call_list_cleared_orders(
    *,
    trading: betfairlightweight.APIClient,
    settled_range: dict,
    from_record: int,
    record_count: int,
    max_retries: int = 5,
) -> Any:
    """
    Call listClearedOrders with retry/backoff for Betfair TIMEOUT_ERROR.
    """
    return retry_betfair_call(
        lambda: trading.betting.list_cleared_orders(
            bet_status="SETTLED",
            settled_date_range=settled_range,
            from_record=from_record,
            record_count=record_count,
            include_item_description=True,
        ),
        max_attempts=max_retries,
    )


def _extract_item_description_fields(order: dict) -> dict:
    """
    Flatten ``itemDescription`` fields from a cleared-order dict into
    top-level columns and remove the raw nested blob.

    If the order has no ``itemDescription`` key the dict is returned
    unchanged.  Missing sub-fields inside the description are silently
    skipped so the function is safe to call on partial responses.
    """
    desc = order.pop("itemDescription", None)
    if not desc or not isinstance(desc, dict):
        return order

    # Required mappings (always expected in a well-formed response)
    _FIELD_MAP = {
        "eventDesc": "evt_eventName",
        "marketDesc": "mkt_marketName",
        "runnerDesc": "runner_name",
        "marketType": "market_type",
    }
    # Optional mappings (may or may not be present)
    _OPTIONAL_MAP = {
        "eachWayDivisor": "each_way_divisor",
        "countryCode": "evt_countryCode",
    }

    for src_key, dest_key in _FIELD_MAP.items():
        if src_key in desc:
            order[dest_key] = desc[src_key]

    for src_key, dest_key in _OPTIONAL_MAP.items():
        if src_key in desc:
            order[dest_key] = desc[src_key]

    return order


def _to_utc_datetime(value: DateLike, *, end_of_day: bool) -> datetime:
    """
    Normalize a ``date`` or ``datetime`` input to a UTC-aware ``datetime``.

    - ``date`` inputs expand to the UTC start-of-day (``end_of_day=False``)
      or the *exclusive* end-of-day — midnight of the following day —
      (``end_of_day=True``), so a full day is covered with no sub-second
      blind spot before midnight.
    - Naive ``datetime`` inputs are assumed to be UTC.
    - tz-aware ``datetime`` inputs are converted to UTC.
    """
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    # plain date
    if end_of_day:
        next_day = value + timedelta(days=1)
        return datetime(
            next_day.year, next_day.month, next_day.day, tzinfo=timezone.utc
        )
    return datetime(value.year, value.month, value.day, 0, 0, 0, tzinfo=timezone.utc)


def _build_datetime_chunks(
    from_dt: datetime,
    to_dt: datetime,
    chunk_days: int,
) -> list[tuple[datetime, datetime]]:
    """
    Split a UTC ``[from_dt, to_dt)`` range into half-open chunks of at most
    ``chunk_days`` calendar days: each chunk's exclusive end is exactly the
    next chunk's start, so orders settled with sub-second precision near
    midnight cannot fall between chunks. The caller's exact start/end
    datetimes are preserved on the first/last chunk.
    """
    date_chunks = chunk_date_range(from_dt.date(), to_dt.date(), chunk_days)
    result: list[tuple[datetime, datetime]] = []
    last = len(date_chunks) - 1
    for i, (cf, ct) in enumerate(date_chunks):
        c_from = (
            from_dt
            if i == 0
            else datetime(cf.year, cf.month, cf.day, 0, 0, 0, tzinfo=timezone.utc)
        )
        if i == last:
            c_to = to_dt
        else:
            next_day = ct + timedelta(days=1)
            c_to = datetime(
                next_day.year, next_day.month, next_day.day, tzinfo=timezone.utc
            )
        result.append((c_from, c_to))
    return result


_REQUIRED_CLEARED_ORDER_COLS: list[str] = [
    "eventTypeId",
    "eventId",
    "marketId",
    "selectionId",
    "handicap",
    "betId",
    "placedDate",
    "persistenceType",
    "orderType",
    "side",
    "betOutcome",
    "priceRequested",
    "settledDate",
    "lastMatchedDate",
    "betCount",
    "priceMatched",
    "priceReduced",
    "sizeSettled",
    "profit",
    "customerOrderRef",
    "customerStrategyRef",
]


def _normalize_cleared_orders_df(df_co: pd.DataFrame) -> pd.DataFrame:
    """
    Apply the same schema normalization the notebook/GUI pipeline relies on:
    ensure required columns exist, preserve extras, add the ``Win`` column, and
    convert ``placedDate`` to Australia/Sydney with derived date/time-only
    columns.
    """
    for c in _REQUIRED_CLEARED_ORDER_COLS:
        if c not in df_co.columns:
            df_co[c] = pd.NA
    df_co = df_co[
        _REQUIRED_CLEARED_ORDER_COLS
        + [c for c in df_co.columns if c not in _REQUIRED_CLEARED_ORDER_COLS]
    ]

    def determine_win(row: pd.Series) -> int:
        if (row["side"] == "BACK" and row["betOutcome"] == "LOST") or (
            row["side"] == "LAY" and row["betOutcome"] == "WON"
        ):
            return 0
        return 1

    if not df_co.empty:
        df_co["Win"] = df_co.apply(determine_win, axis=1)
    else:
        df_co["Win"] = pd.Series(dtype="int")

    df_co["placedDate"] = pd.to_datetime(df_co["placedDate"], utc=True, errors="coerce")
    aet_zone = ZoneInfo("Australia/Sydney")
    df_co["placedDate"] = df_co["placedDate"].dt.tz_convert(aet_zone)
    df_co["placedDateOnly"] = df_co["placedDate"].dt.date
    df_co["placedTimeOnly"] = df_co["placedDate"].dt.time
    return df_co


def fetch_cleared_orders_df_range(
    *,
    betfair: dict[str, Any],
    from_date: DateLike,
    to_date: DateLike,
    chunk_days: int = 30,
    api_client: Optional[betfairlightweight.APIClient] = None,
    page_size: int = 200,
    status_cb: Optional[Callable[[str], None]] = None,
) -> DownloadResult:
    """
    Download settled (cleared) orders for an explicit date range, chunked into
    safe Betfair settledDateRange windows.

    Parameters
    ----------
    betfair:
        Betfair credentials dict (``username``/``password``/``app_key``, and
        optionally ``certs_dir``). Only consulted if ``api_client`` is ``None``.
    from_date, to_date:
        Inclusive range. ``date`` inputs expand to full-day UTC (``to_date``
        becomes an exclusive bound at midnight of the following day, so the
        whole final day is covered); ``datetime`` is used as-is (naive
        datetimes are assumed UTC).
    chunk_days:
        Max calendar days per API call (default 30). Betfair's
        ``listClearedOrders`` settledDateRange becomes increasingly timeout-prone
        on wider windows; 30 is a conservative default.
    api_client:
        Optional pre-authenticated ``betfairlightweight.APIClient``. When
        provided (e.g. from ``scheduler.auth.build_api_client``), the caller
        owns it and we will NOT log it out. When ``None``, this function falls
        back to ``login_interactive()`` to preserve legacy GUI behaviour and
        logs out its own client at the end.
    page_size:
        Records per ``listClearedOrders`` page (default 200, matches legacy).
    status_cb:
        Optional progress callback invoked with short human-readable strings,
        once per chunk. Not called by the legacy ``fetch_cleared_orders_df``
        delegator (kept silent there for backward compatibility).
    """
    username = (betfair.get("username") or "").strip()
    password = betfair.get("password") or ""
    app_key = (betfair.get("app_key") or "").strip()

    if api_client is None and not (username and password and app_key):
        return DownloadResult(
            attempted=False,
            rows_downloaded=0,
            message="Betfair credentials missing (username/password/app_key).",
            df_co=None,
        )

    from_dt = _to_utc_datetime(from_date, end_of_day=False)
    to_dt = _to_utc_datetime(to_date, end_of_day=True)

    if from_dt >= to_dt:
        empty = _normalize_cleared_orders_df(pd.DataFrame())
        return DownloadResult(
            attempted=True,
            rows_downloaded=0,
            message=f"Empty date range: {from_dt.date()} >= {to_dt.date()}.",
            df_co=empty,
        )

    chunks = _build_datetime_chunks(from_dt, to_dt, chunk_days)

    # Auth: reuse caller-provided client (scheduler path) or build+login one
    # ourselves using the legacy interactive flow (GUI/CLI path).
    owns_client = False
    trading: betfairlightweight.APIClient
    if api_client is not None:
        trading = api_client
    else:
        trading = betfairlightweight.APIClient(
            username=username,
            password=password,
            app_key=app_key,
        )
        trading.login_interactive()
        owns_client = True

    all_rows: list[dict[str, Any]] = []
    try:
        total_chunks = len(chunks)
        for idx, (c_from, c_to) in enumerate(chunks, start=1):
            if status_cb and total_chunks > 1:
                try:
                    status_cb(
                        f"Download chunk {idx}/{total_chunks}: "
                        f"{c_from.strftime('%Y-%m-%d %H:%M')} -> {c_to.strftime('%Y-%m-%d %H:%M')} (exclusive)"
                    )
                except Exception:
                    pass

            settled_range = betfairlightweight.filters.time_range(
                from_=c_from.strftime("%Y-%m-%dT%H:%M:%SZ"),
                to=c_to.strftime("%Y-%m-%dT%H:%M:%SZ"),
            )

            indexrecord = 0
            # Betfair says whether more pages exist; asking for one page past
            # the end just to receive an empty list wastes a round-trip per
            # chunk, and an API that clamped from_record instead of returning
            # empty would loop a launchd job forever. Cap iterations so no
            # response shape can spin unattended.
            max_pages = 10_000
            for _page in range(max_pages):
                cleared_orders = _call_list_cleared_orders(
                    trading=trading,
                    settled_range=settled_range,
                    from_record=indexrecord,
                    record_count=page_size,
                )
                data = json.loads(cleared_orders.json())
                batch = data.get("clearedOrders", []) or []
                if not batch:
                    break
                all_rows.extend(_extract_item_description_fields(row) for row in batch)
                indexrecord += page_size
                # Honour an explicit "no more pages"; an absent key falls back
                # to the legacy paginate-until-empty behaviour rather than
                # guessing, so a response missing the field cannot truncate a
                # download.
                if data.get("moreAvailable") is False:
                    break
            else:
                raise RuntimeError(
                    f"Cleared-orders pagination exceeded {max_pages:,} pages for "
                    f"one chunk; aborting rather than looping unattended."
                )
    finally:
        if owns_client:
            try:
                trading.logout()
            except Exception:
                pass

    df_co = _normalize_cleared_orders_df(pd.DataFrame(all_rows))

    # to_dt is an exclusive bound; show the last covered instant's date.
    to_display = (to_dt - timedelta(microseconds=1)).date()
    return DownloadResult(
        attempted=True,
        rows_downloaded=len(df_co),
        message=(
            f"Downloaded cleared orders: {len(df_co):,} rows "
            f"(range={from_dt.date()}->{to_display}, chunks={len(chunks)})."
        ),
        df_co=df_co,
        from_utc=from_dt,
        to_utc=to_dt,
    )


# -----------------------------
# Enrichment: market catalogue
# -----------------------------


def _call_list_market_catalogue(
    *,
    trading: betfairlightweight.APIClient,
    market_ids: list[str],
    max_retries: int = 5,
) -> Any:
    """
    Call listMarketCatalogue with the same TIMEOUT_ERROR retry/backoff policy
    as :func:`_call_list_cleared_orders`.
    """
    return retry_betfair_call(
        lambda: trading.betting.list_market_catalogue(
            filter=filters.market_filter(market_ids=market_ids),
            max_results=1000,
            market_projection=["MARKET_START_TIME", "EVENT"],
        ),
        max_attempts=max_retries,
    )


def coalesce_catalogue_columns(
    df_work: pd.DataFrame, df_catalogue: pd.DataFrame
) -> pd.DataFrame:
    """
    Merge catalogue metadata onto ``df_work`` by marketId, preferring
    values already present (extracted from itemDescription) and filling
    only the gaps from the catalogue. Never leaves ``*_cat`` suffix
    columns behind, and columns the work frame lacks entirely come
    across from the catalogue as-is.
    """
    overlap_cols = [
        c for c in df_catalogue.columns if c != "marketId" and c in df_work.columns
    ]
    df_out = df_work.merge(
        df_catalogue, on="marketId", how="left", suffixes=("", "_cat")
    )
    for col in overlap_cols:
        cat_col = f"{col}_cat"
        if cat_col in df_out.columns:
            df_out[col] = df_out[col].fillna(df_out[cat_col])
            df_out.drop(columns=[cat_col], inplace=True)
    return df_out


def enrich_with_market_catalogue(
    *,
    df_co: pd.DataFrame,
    betfair: dict[str, Any],
    cache_dir: Path,
    enable: bool = True,
    use_cache: bool = True,
    batch_size: int = 50,
    sleep_seconds: float = 0.20,
    status_cb: Optional[Callable[[str], None]] = None,
    api_client: Optional[betfairlightweight.APIClient] = None,
) -> tuple[pd.DataFrame, EnrichResult]:
    """
    Enrich cleared orders with market catalogue metadata:
    - list_market_catalogue by marketId batches (timeout retry/backoff)
    - cache at <cache_dir>/market_catalogue_event_cache.csv
    - deterministic column names: mkt_*, evt_*

    A mid-fetch API failure does not raise: rows fetched before the failure
    are still written to the cache and merged, and the failure is reported in
    the returned :class:`EnrichResult` message.
    """

    def say(msg: str) -> None:
        if status_cb:
            try:
                status_cb(msg)
            except Exception:
                pass

    if (not enable) or df_co is None or df_co.empty:
        return df_co, EnrichResult(
            attempted=True,
            markets_requested=0,
            markets_returned=0,
            message="Enrichment skipped (disabled or empty df).",
            unique_market_ids=0,
            use_cache=use_cache,
            batch_size=batch_size,
        )

    username = (betfair.get("username") or "").strip()
    password = betfair.get("password") or ""
    app_key = (betfair.get("app_key") or "").strip()

    if api_client is None and (not username or not password or not app_key):
        return df_co, EnrichResult(
            attempted=False,
            markets_requested=0,
            markets_returned=0,
            message="Enrichment skipped (Betfair creds missing).",
            unique_market_ids=int(df_co["marketId"].nunique())
            if "marketId" in df_co.columns
            else 0,
            use_cache=use_cache,
            batch_size=batch_size,
        )

    # Reuse caller-provided session (scheduler path; eliminates double-login)
    # or fall back to the legacy interactive login (GUI path, unchanged).
    if api_client is not None:
        trading = api_client
    else:
        trading = betfairlightweight.APIClient(
            username=username, password=password, app_key=app_key
        )
        trading.login_interactive()

    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_path = cache_dir / "market_catalogue_event_cache.csv"
    snapshot_path = cache_dir / "market_catalogue_event_latest.csv"

    say(f"Enrichment cache directory: {cache_dir}")

    df_work = df_co.copy()
    df_work["marketId"] = df_work["marketId"].astype(str)

    if use_cache and cache_path.exists():
        df_cache = pd.read_csv(cache_path, dtype=str)
    else:
        df_cache = pd.DataFrame()

    unique_market_ids = sorted(df_work["marketId"].dropna().unique().tolist())
    cached_market_ids = (
        set(df_cache["marketId"].unique())
        if (not df_cache.empty and "marketId" in df_cache.columns)
        else set()
    )
    missing_market_ids = [m for m in unique_market_ids if m not in cached_market_ids]

    cache_hits = len(unique_market_ids) - len(missing_market_ids)
    cache_misses = len(missing_market_ids)

    say(
        f"Enrichment: unique_market_ids={len(unique_market_ids):,}, cache_hits={cache_hits:,}, cache_misses={cache_misses:,}."
    )

    fetched_rows: list[dict[str, Any]] = []
    returned_total = 0
    fetch_error: Optional[str] = None

    total_batches = (
        (len(missing_market_ids) + batch_size - 1) // batch_size
        if batch_size > 0
        else 0
    )
    try:
        for idx, batch in enumerate(_chunked(missing_market_ids, batch_size), start=1):
            if total_batches > 0:
                say(
                    f"Enrichment: fetching batch {idx}/{total_batches} (batch_size={len(batch):,})..."
                )
            time.sleep(sleep_seconds)

            cats = _call_list_market_catalogue(trading=trading, market_ids=batch)
            returned_total += len(cats)

            for cat in cats:
                fetched_rows.append(
                    {
                        "marketId": str(cat.market_id),
                        "mkt_marketName": getattr(cat, "market_name", None),
                        "mkt_marketStartTime": getattr(cat, "market_start_time", None),
                        "evt_eventId": str(cat.event.id)
                        if getattr(cat, "event", None)
                        else None,
                        "evt_eventName": cat.event.name
                        if getattr(cat, "event", None)
                        else None,
                        "evt_countryCode": cat.event.country_code
                        if getattr(cat, "event", None)
                        else None,
                    }
                )
    except Exception as exc:
        # Salvage what was fetched: cache it, merge it, and report the failure
        # instead of losing the whole run to a transient catalogue error.
        fetch_error = f"{type(exc).__name__}: {exc}"
        say(
            f"Enrichment fetch aborted after {len(fetched_rows):,} rows "
            f"({returned_total:,} catalogues): {fetch_error}"
        )

    df_fetched = pd.DataFrame(fetched_rows)

    if not df_fetched.empty:
        df_new_cache = (
            pd.concat([df_cache, df_fetched], ignore_index=True)
            if not df_cache.empty
            else df_fetched
        )
        df_new_cache = df_new_cache.drop_duplicates(subset=["marketId"], keep="last")
        df_new_cache.to_csv(cache_path, index=False)
        df_new_cache.to_csv(snapshot_path, index=False)
    else:
        df_new_cache = df_cache
        if not df_cache.empty:
            df_cache.to_csv(snapshot_path, index=False)

    cache_rows = len(df_new_cache) if not df_new_cache.empty else 0

    if not df_new_cache.empty:
        df_out = coalesce_catalogue_columns(df_work, df_new_cache)

        # --- Message clarity tweak (cache-only vs API) ---
        if returned_total == 0 and cache_hits > 0:
            msg = f"Enriched metadata from cache only (API returned 0). Cache rows={cache_rows:,}."
        elif returned_total > 0 and cache_hits > 0:
            msg = f"Enriched metadata from cache + API. Cache rows={cache_rows:,}, api_returned={returned_total:,}."
        elif returned_total > 0:
            msg = f"Enriched metadata from API (no cache hits). Cache rows={cache_rows:,}, api_returned={returned_total:,}."
        else:
            msg = (
                f"Enriched metadata using market catalogue. Cache rows={cache_rows:,}."
            )
        if fetch_error:
            msg += f" WARNING: catalogue fetch aborted early ({fetch_error})."

        return df_out, EnrichResult(
            attempted=True,
            markets_requested=len(missing_market_ids),
            markets_returned=returned_total,
            message=msg,
            unique_market_ids=len(unique_market_ids),
            use_cache=use_cache,
            cache_path=str(cache_path),
            cache_snapshot_path=str(snapshot_path),
            cache_rows=cache_rows,
            cache_hits=cache_hits,
            cache_misses=cache_misses,
            batch_size=batch_size,
        )

    no_rows_msg = "No enrichment rows available (cache empty and fetch returned none)."
    if fetch_error:
        no_rows_msg += f" WARNING: catalogue fetch aborted early ({fetch_error})."
    return df_work, EnrichResult(
        attempted=True,
        markets_requested=len(missing_market_ids),
        markets_returned=returned_total,
        message=no_rows_msg,
        unique_market_ids=len(unique_market_ids),
        use_cache=use_cache,
        cache_path=str(cache_path),
        cache_snapshot_path=str(snapshot_path),
        cache_rows=cache_rows,
        cache_hits=cache_hits,
        cache_misses=cache_misses,
        batch_size=batch_size,
    )


# -----------------------------
# CSV outputs
# -----------------------------


_SNAPSHOT_NAME_RE = re.compile(
    r"^cleared_orders_cleaned_(\d{4}-\d{2}-\d{2})\.csv(\.gz)?$"
)


def _write_snapshot_from_canonical(canonical_path: Path, snapshot_path: Path) -> None:
    """
    Copy the canonical to the dated snapshot, gzipping when the name asks for
    it. Written to a temp file and renamed, so an interrupted run cannot leave
    a truncated snapshot that still matches the retention pattern and displaces
    a good one.
    """
    tmp_path = snapshot_path.with_name(snapshot_path.name + ".tmp")
    try:
        if snapshot_path.suffix == ".gz":
            with (
                open(canonical_path, "rb") as src,
                gzip.open(tmp_path, "wb") as dst,
            ):
                shutil.copyfileobj(src, dst, length=1024 * 1024)
        else:
            shutil.copyfile(canonical_path, tmp_path)
        tmp_path.replace(snapshot_path)
    finally:
        if tmp_path.exists():
            tmp_path.unlink()


def prune_snapshot_files(
    results_csv_dir: Path,
    keep: int = 14,
    status_cb: Optional[Callable[[str], None]] = None,
) -> list[Path]:
    """
    Delete dated snapshot files beyond the ``keep`` most recent.

    Only files matching ``cleared_orders_cleaned_YYYY-MM-DD.csv[.gz]`` are
    considered; the canonical file is never touched. ``keep <= 0`` disables
    pruning. Returns the list of deleted paths.
    """
    if keep <= 0:
        return []

    snapshots: list[tuple[str, Path]] = []
    for f in results_csv_dir.iterdir():
        m = _SNAPSHOT_NAME_RE.match(f.name)
        if m:
            snapshots.append((m.group(1), f))

    snapshots.sort(key=lambda t: (t[0], t[1].name), reverse=True)
    stale = [path for _, path in snapshots[keep:]]

    deleted: list[Path] = []
    for path in stale:
        try:
            path.unlink()
            deleted.append(path)
        except OSError as exc:
            if status_cb:
                status_cb(f"Snapshot prune: could not delete {path.name}: {exc}")

    if deleted and status_cb:
        status_cb(
            f"Snapshot prune: deleted {len(deleted)} snapshot(s) older than the "
            f"{keep} most recent."
        )
    return deleted


def archive_old_canonical_rows(
    df_canonical: pd.DataFrame,
    results_csv_dir: Path,
    archive_months: int = 12,
    status_cb: Optional[Callable[[str], None]] = None,
) -> pd.DataFrame:
    """
    Move rows settled more than ``archive_months`` ago into yearly compressed
    archives (``cleared_orders_archive_YYYY.csv.gz``) and return the trimmed
    canonical dataframe.

    Rows with an unparseable ``settledDate`` are always kept in the canonical.
    Archives are deduplicated on append, so re-running after a partial failure
    is safe. ``archive_months <= 0`` disables archival.
    """
    if (
        archive_months <= 0
        or df_canonical.empty
        or "settledDate" not in df_canonical.columns
    ):
        return df_canonical

    settled = pd.to_datetime(
        df_canonical["settledDate"], utc=True, errors="coerce", format="ISO8601"
    )
    cutoff = pd.Timestamp(datetime.now(timezone.utc)) - pd.DateOffset(
        months=archive_months
    )
    old_mask = settled.notna() & (settled < cutoff)
    if not old_mask.any():
        return df_canonical

    for year in sorted(settled[old_mask].dt.year.unique()):
        year_mask = old_mask & (settled.dt.year == year)
        chunk = df_canonical[year_mask]
        moving = int(year_mask.sum())
        archive_path = results_csv_dir / f"cleared_orders_archive_{year}.csv.gz"

        if archive_path.exists():
            # dtype=str for the same reason as the canonical: inferred types
            # silently truncate marketId, and the archive is the only
            # remaining copy of these rows.
            existing = pd.read_csv(archive_path, dtype=str, keep_default_na=False)
            cols = sorted(set(existing.columns).union(set(chunk.columns)))
            chunk = pd.concat(
                [existing.reindex(columns=cols), chunk.reindex(columns=cols)],
                ignore_index=True,
            )
        chunk = clean_and_remove_duplicates(chunk, status_cb=status_cb)

        # These rows are about to be deleted from the canonical, and the
        # snapshots are written from the trimmed frame, so the archive is the
        # only place they will exist. Prove each one is actually there.
        #
        # Membership, not row count: re-archiving a row that is already in the
        # archive is a legitimate no-op, so the archive growing by less than
        # the number moved is expected. What is not acceptable is a row
        # leaving the canonical without arriving here.
        leaving = df_canonical.loc[year_mask]
        if "betId" in leaving.columns and "betId" in chunk.columns:
            # Same normalisation as the dedupe key, so a legacy "123.0" and a
            # fresh 123 are recognised as one record rather than a loss.
            missing = betid_keys(leaving) - betid_keys(chunk)
            if missing:
                raise ValueError(
                    f"Refusing to archive into {archive_path.name}: "
                    f"{len(missing):,} of the {moving:,} rows selected for "
                    f"{year} are absent from the archive after the merge and "
                    f"would be deleted from the canonical without being stored "
                    f"anywhere (e.g. betId {sorted(missing)[:3]}). The "
                    f"canonical has been left untouched."
                )

        # Rows with no usable betId cannot be checked by key, so count them.
        def _keyless(frame):
            """Rows the dedupe key cannot identify, so they cannot be checked by key."""
            if "betId" not in frame.columns:
                return len(frame)
            return int(pd.to_numeric(frame["betId"], errors="coerce").isna().sum())

        if _keyless(chunk) < _keyless(leaving):
            raise ValueError(
                f"Refusing to archive into {archive_path.name}: "
                f"{_keyless(leaving):,} rows without a usable betId were "
                f"selected for {year} but only {_keyless(chunk):,} are present "
                f"in the archive. The canonical has been left untouched."
            )

        tmp_path = archive_path.with_name(archive_path.name + ".tmp")
        chunk.to_csv(tmp_path, index=False, compression="gzip")
        tmp_path.replace(archive_path)
        if status_cb:
            status_cb(
                f"Archive: moved {moving:,} rows settled before "
                f"{cutoff.date()} into {archive_path.name} (now {len(chunk):,} rows)."
            )

    return df_canonical[~old_mask].reset_index(drop=True)


def write_csv_outputs(
    *,
    df_co: pd.DataFrame,
    results_csv_dir: Path,
    status_cb: Optional[Callable[[str], None]] = None,
    snapshot_retention: int = 14,
    compress_snapshots: bool = True,
    archive_months: int = 12,
) -> CsvWriteResult:
    """
    Notebook Cells 7–8, ported:
    - canonical: cleared_orders_cleaned.csv (idempotent update)
    - archival: rows older than ``archive_months`` move to yearly .csv.gz archives
    - snapshot: cleared_orders_cleaned_YYYY-MM-DD.csv[.gz] (copy of canonical)
    - retention: dated snapshots beyond ``snapshot_retention`` are deleted
    """
    results_csv_dir.mkdir(parents=True, exist_ok=True)

    canonical_path = results_csv_dir / "cleared_orders_cleaned.csv"
    today_str = datetime.now(timezone.utc).date().isoformat()
    suffix = ".csv.gz" if compress_snapshots else ".csv"
    snapshot_path = results_csv_dir / f"cleared_orders_cleaned_{today_str}{suffix}"

    canonical_path, df_canonical = update_csv_with_new_data(
        canonical_path, df_co, status_cb=status_cb
    )
    df_trimmed = archive_old_canonical_rows(
        df_canonical,
        results_csv_dir,
        archive_months=archive_months,
        status_cb=status_cb,
    )
    if len(df_trimmed) < len(df_canonical):
        tmp_path = canonical_path.with_suffix(canonical_path.suffix + ".tmp")
        df_trimmed.to_csv(tmp_path, index=False)
        tmp_path.replace(canonical_path)

    # The snapshot is a copy of the canonical, so copy it rather than
    # re-serialising the frame a second time. Two things follow.
    #
    # It is much cheaper: re-serialising ~1M rows and gzipping costs ~9s on
    # the live file, against ~2s to stream the bytes that were just written.
    #
    # And it is exact. Serialising a second time round-tripped the frame
    # through pandas dtype inference, so numeric-looking string columns
    # diverged between the canonical and its own backup -- 12345678 in one,
    # 12345678.0 in the other, on the very first write.
    _write_snapshot_from_canonical(canonical_path, snapshot_path)

    prune_snapshot_files(results_csv_dir, keep=snapshot_retention, status_cb=status_cb)

    return CsvWriteResult(
        canonical_path=canonical_path,
        snapshot_path=snapshot_path,
        rows_in_canonical=len(df_trimmed),
        message=f"Wrote canonical + snapshot CSV. canonical_rows={len(df_trimmed):,}.",
        df_canonical=df_trimmed,
    )


# -----------------------------
# Azure prep (filter + aggregate + rows_to_write)
# -----------------------------


# Kept under its historical name: the Azure selection and aggregation code
# (and its tests) refer to it, and the reporting layer now shares the same
# key via csv_utils.
_decimal_key = decimal_key


def _rows_not_already_held(base: pd.DataFrame, candidate: pd.DataFrame) -> pd.DataFrame:
    """
    Candidate rows whose bets ``base`` does not already hold.

    Keyed bets match on :func:`_decimal_key` -- the canonical dedupe's
    numeric semantics, so legacy "123.0" and fresh 123 are one bet and
    cannot double-count. A bet with an unusable betId has no identity
    beyond its full row -- exactly how clean_and_remove_duplicates
    preserves such rows -- so keyless candidates match by full-row
    equality over the shared columns instead of collapsing onto a
    shared "" key.
    """
    if candidate is None or candidate.empty:
        return pd.DataFrame()
    if base is None or base.empty:
        return candidate
    if "betId" not in base.columns or "betId" not in candidate.columns:
        # pragma: no cover - both frames always carry betId in practice
        base_ids = set(base["marketId"].map(_decimal_key))
        return candidate[~candidate["marketId"].map(_decimal_key).isin(base_ids)]
    base_numeric = pd.to_numeric(base["betId"], errors="coerce")
    cand_numeric = pd.to_numeric(candidate["betId"], errors="coerce")
    held = set(base["betId"][base_numeric.notna()].map(_decimal_key))
    keyed_mask = cand_numeric.notna()
    extra_keyed = candidate[
        keyed_mask & ~candidate["betId"].map(_decimal_key).isin(held)
    ]
    keyless = candidate[~keyed_mask]
    if len(keyless):
        common = [c for c in candidate.columns if c in base.columns]
        base_keyless_rows = set(
            map(tuple, base[base_numeric.isna()][common].astype(str).values)
        )
        keep = [
            tuple(r) not in base_keyless_rows
            for r in keyless[common].astype(str).values
        ]
        keyless = keyless[keep]
    return pd.concat([extra_keyed, keyless])


def _window_may_touch_archives(df_window: pd.DataFrame, archive_months: int) -> bool:
    """
    Whether the window could involve bets old enough to sit in a yearly
    archive -- the gate that keeps four daily scheduled runs from
    re-reading every archive: their markets settled today, nowhere near
    the cutoff. Stays conservative (True) when there is no settledDate
    to reason with or any of them fail to parse.
    """
    if archive_months <= 0:
        return True  # no cutoff to reason with; the glob guard still applies
    if "settledDate" not in df_window.columns:
        return True
    settled = pd.to_datetime(
        df_window["settledDate"], utc=True, errors="coerce", format="ISO8601"
    )
    if bool(settled.isna().any()):
        return True
    cutoff = pd.Timestamp(datetime.now(timezone.utc)) - pd.DateOffset(
        months=archive_months
    )
    # Sixty days of slack over the cutoff: all bets of one market settle
    # within days of each other (a market settles as one event; split
    # settlements span days at most), so a market with archived bets
    # cannot also settle a fresh bet months later -- but a window
    # straddling the boundary by mere weeks must still trigger the read.
    return bool((settled < cutoff + pd.DateOffset(days=60)).any())


def _archived_rows_for_markets(
    results_csv_dir: Path, market_keys: set[str]
) -> list[pd.DataFrame]:
    """
    Rows from the yearly archives whose market the window touched.

    Best-effort by explicit trade-off: an unreadable archive logs a
    warning and is skipped rather than blocking the publish. Archives
    hold rows settled over a year ago, and a scheduled run's markets
    settled today -- the only run that could be understated by a skipped
    archive is a manual backfill of ancient dates, and the warning names
    the file to re-run afterwards.
    """
    hits: list[pd.DataFrame] = []
    try:
        paths = sorted(Path(results_csv_dir).glob("cleared_orders_archive_*.csv.gz"))
    except OSError:
        return hits
    for path in paths:
        try:
            # Full rows, not an Azure-column projection: keyless bets are
            # deduplicated by full-row identity, and a projection would
            # make two different bets look identical.
            df = pd.read_csv(path, dtype=str, keep_default_na=False)
        except (OSError, ValueError) as exc:
            logger.warning(
                "Could not read archive %s for Azure aggregation: %s -- "
                "skipping it; re-run the backfill after fixing the file if "
                "this window touched pre-archive markets.",
                path.name,
                exc,
            )
            continue
        if df.empty or "marketId" not in df.columns:
            continue
        hit = df[df["marketId"].map(_decimal_key).isin(market_keys)]
        if len(hit):
            hits.append(hit)
    return hits


def select_azure_rows_from_canonical(
    df_canonical: Optional[pd.DataFrame],
    df_window: pd.DataFrame,
    *,
    results_csv_dir: Optional[Path] = None,
    archive_months: int = 12,
) -> pd.DataFrame:
    """
    Return every known bet for the marketIds the window touched.

    A market can settle across two download windows (a partial cash-out
    today, the remainder tomorrow). Aggregating the window frame alone
    would overwrite that market's Azure profit with only the latest
    window's bets, so the aggregation gathers, without double-counting:

    - the canonical rows for the touched markets (normalized market
      keys: historical rows can hold float-damaged spellings of the
      same market, see :func:`_decimal_key`);
    - window rows the canonical does not hold (a backfill can download
      rows old enough that write_csv_outputs archives them straight
      back out);
    - rows already sitting in the yearly archives for those markets,
      when ``results_csv_dir`` is given AND the window contains bets
      old enough to plausibly be archived (bets archived by an EARLIER
      run are in neither the canonical nor the window; scheduled runs
      settle today and skip the archive read entirely).

    Falls back to the window frame when the canonical is unavailable,
    which preserves the old behaviour rather than skipping the publish.
    """
    if df_window is None or df_window.empty or "marketId" not in df_window.columns:
        return df_window
    window_ids = set(df_window["marketId"].map(_decimal_key))
    if (
        df_canonical is None
        or df_canonical.empty
        or "marketId" not in df_canonical.columns
    ):
        selected = df_window
    else:
        canonical_ids = df_canonical["marketId"].map(_decimal_key)
        selected = df_canonical[canonical_ids.isin(window_ids)]
        extra = _rows_not_already_held(selected, df_window)
        if len(extra):
            selected = pd.concat([selected, extra], ignore_index=True)
    if results_csv_dir is not None and _window_may_touch_archives(
        df_window, archive_months
    ):
        for archived in _archived_rows_for_markets(results_csv_dir, window_ids):
            extra = _rows_not_already_held(selected, archived)
            if len(extra):
                selected = pd.concat([selected, extra], ignore_index=True)
    return selected.copy()


def _money2(x: Any) -> Decimal:
    return Decimal(str(x)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


# Azure publishing is fixed to Horse Racing (7) and Greyhound Racing (4339).
DEFAULT_AZURE_EVENT_TYPE_IDS: frozenset[int] = frozenset(
    {EVENTTYPE_HORSES, EVENTTYPE_GREYHOUNDS}
)


def prepare_azure_dataset(
    *,
    df_co: pd.DataFrame,
    allowed_event_type_ids: Optional[frozenset[int]] = None,
) -> AzurePrepResult:
    """
    Prepare the Azure upload dataset:
    - filter df_co to allowed eventTypeIds (numeric coercion); defaults to
      :data:`DEFAULT_AZURE_EVENT_TYPE_IDS` (horses + greyhounds)
    - aggregate by marketId (sum profit, count betId, min/max placedDate)
    - build rows_to_write list: (MarketID decimal, Profit decimal(2), Notes)
    """
    if allowed_event_type_ids is None:
        allowed_event_type_ids = DEFAULT_AZURE_EVENT_TYPE_IDS
    required_cols = {"eventTypeId", "marketId", "profit", "betId", "placedDate"}
    missing = required_cols - set(df_co.columns)
    if missing:
        return AzurePrepResult(
            False, 0, 0, f"Azure prep failed: missing columns {sorted(missing)}."
        )

    df_stage = df_co.copy()
    df_stage["eventTypeId"] = pd.to_numeric(
        df_stage["eventTypeId"], errors="coerce"
    ).astype("Int64")

    df_azure_upload = df_stage[
        df_stage["eventTypeId"].isin(list(allowed_event_type_ids))
    ].copy()
    # The canonical is read with dtype=str; summing string profits would
    # concatenate them. Coerce here so both sources aggregate identically
    # -- but a value that FAILS to coerce must abort preparation, exactly
    # as it aborted _money2 before: pandas would otherwise skip the NaN
    # and publish an understated market total as a success. Validated
    # after the sport filter, so a malformed profit in a sport Azure
    # never publishes cannot block the racing markets that do.
    original_profit = df_azure_upload["profit"]
    df_azure_upload["profit"] = pd.to_numeric(original_profit, errors="coerce")
    # Null AND unparseable both abort: a publishable racing row with no
    # usable profit means the sum would silently understate the market.
    bad_profit = df_azure_upload["profit"].isna()
    if bool(bad_profit.any()):
        example = original_profit[bad_profit].iloc[0]
        return AzurePrepResult(
            False,
            0,
            0,
            f"Azure prep failed: {int(bad_profit.sum())} missing or "
            f"unparseable profit value(s), e.g. {example!r}.",
        )
    # Group on losslessly normalized market keys: 8.6% of historical rows
    # carry float-damaged spellings (1.2515001 for 1.251500100), and both
    # spellings are the same Decimal MarketID in Azure. Grouping raw
    # strings would split one market into two rows_to_write.
    df_azure_upload["marketId"] = df_azure_upload["marketId"].map(_decimal_key)
    if df_azure_upload.empty:
        return AzurePrepResult(
            attempted=True,
            rows_after_filter=0,
            markets_aggregated=0,
            message="Azure prep produced 0 rows after filtering to allowed eventTypeIds.",
            df_market_results=None,
            rows_to_write=None,
        )

    # No placedDate min/max aggregates: nothing downstream reads them,
    # and the mixed frame (canonical rows carry string dates, freshly
    # downloaded rows carry Timestamps) would make min/max raise
    # TypeError on incomparable values.
    df_market_results = df_azure_upload.groupby("marketId", as_index=False).agg(
        Profit=("profit", "sum"),
        Bets=("betId", "count"),
    )

    rows_to_write: list[tuple[Decimal, Decimal, str]] = []
    for _, r in df_market_results.iterrows():
        market_id = Decimal(str(r["marketId"]))
        profit = _money2(r["Profit"])
        rows_to_write.append((market_id, profit, ""))

    return AzurePrepResult(
        attempted=True,
        rows_after_filter=len(df_azure_upload),
        markets_aggregated=len(df_market_results),
        message=f"Prepared Azure dataset: {len(df_azure_upload):,} rows -> {len(df_market_results):,} markets.",
        df_market_results=df_market_results,
        rows_to_write=rows_to_write,
    )

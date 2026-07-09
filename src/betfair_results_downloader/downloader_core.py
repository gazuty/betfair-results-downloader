from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Callable, Iterable, List, Optional, Tuple, Union

import json
import re
import time

import pandas as pd
import pytz

import betfairlightweight
from betfairlightweight import filters
from betfairlightweight.exceptions import APIError

from .csv_utils import clean_and_remove_duplicates, update_csv_with_new_data
from .scheduler.date_windows import chunk_date_range


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
    from_utc: Optional[str] = None
    to_utc: Optional[str] = None


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
    for attempt in range(1, max_retries + 1):
        try:
            return trading.betting.list_cleared_orders(
                bet_status="SETTLED",
                settled_date_range=settled_range,
                from_record=from_record,
                record_count=record_count,
                include_item_description=False,  # verbose JSON blob (~30% of CSV size); names come from enrichment columns instead
            )
        except APIError as e:
            msg = str(e)
            is_timeout = ("TIMEOUT_ERROR" in msg) or ("ANGX-0010" in msg)
            if (not is_timeout) or attempt == max_retries:
                raise
            sleep_s = min(2**attempt, 20)
            time.sleep(sleep_s)


def _to_utc_datetime(value: DateLike, *, end_of_day: bool) -> datetime:
    """
    Normalize a ``date`` or ``datetime`` input to a UTC-aware ``datetime``.

    - ``date`` inputs expand to the UTC start-of-day (``end_of_day=False``)
      or end-of-day (``23:59:59`` UTC, ``end_of_day=True``).
    - Naive ``datetime`` inputs are assumed to be UTC.
    - tz-aware ``datetime`` inputs are converted to UTC.
    """
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    # plain date
    if end_of_day:
        return datetime(value.year, value.month, value.day, 23, 59, 59, tzinfo=timezone.utc)
    return datetime(value.year, value.month, value.day, 0, 0, 0, tzinfo=timezone.utc)


def _build_datetime_chunks(
    from_dt: datetime,
    to_dt: datetime,
    chunk_days: int,
) -> list[tuple[datetime, datetime]]:
    """
    Split a UTC ``[from_dt, to_dt]`` range into chunks of at most ``chunk_days``
    calendar days, preserving the caller's exact start/end datetime on the
    first/last chunk (so legacy callers that pass sub-day precision see
    identical API-window boundaries when the whole range fits in one chunk).
    """
    date_chunks = chunk_date_range(from_dt.date(), to_dt.date(), chunk_days)
    result: list[tuple[datetime, datetime]] = []
    last = len(date_chunks) - 1
    for i, (cf, ct) in enumerate(date_chunks):
        c_from = from_dt if i == 0 else datetime(cf.year, cf.month, cf.day, 0, 0, 0, tzinfo=timezone.utc)
        c_to = to_dt if i == last else datetime(ct.year, ct.month, ct.day, 23, 59, 59, tzinfo=timezone.utc)
        result.append((c_from, c_to))
    return result


_REQUIRED_CLEARED_ORDER_COLS: list[str] = [
    "eventTypeId", "eventId", "marketId", "selectionId", "handicap", "betId",
    "placedDate", "persistenceType", "orderType", "side", "betOutcome",
    "priceRequested", "settledDate", "lastMatchedDate", "betCount",
    "priceMatched", "priceReduced", "sizeSettled", "profit",
    "customerOrderRef", "customerStrategyRef",
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
    df_co = df_co[_REQUIRED_CLEARED_ORDER_COLS + [c for c in df_co.columns if c not in _REQUIRED_CLEARED_ORDER_COLS]]

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
    aet_zone = pytz.timezone("Australia/Sydney")
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
        Inclusive range. ``date`` expands to full-day UTC; ``datetime`` is used
        as-is (naive datetimes are assumed UTC).
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

    if from_dt > to_dt:
        empty = _normalize_cleared_orders_df(pd.DataFrame())
        return DownloadResult(
            attempted=True,
            rows_downloaded=0,
            message=f"Empty date range: {from_dt.date()} > {to_dt.date()}.",
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
                        f"{c_from.strftime('%Y-%m-%d')} -> {c_to.strftime('%Y-%m-%d')}"
                    )
                except Exception:
                    pass

            settled_range = betfairlightweight.filters.time_range(
                from_=c_from.strftime("%Y-%m-%dT%H:%M:%SZ"),
                to=c_to.strftime("%Y-%m-%dT%H:%M:%SZ"),
            )

            indexrecord = 0
            while True:
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
                all_rows.extend(batch)
                indexrecord += page_size
    finally:
        if owns_client:
            try:
                trading.logout()
            except Exception:
                pass

    df_co = _normalize_cleared_orders_df(pd.DataFrame(all_rows))

    return DownloadResult(
        attempted=True,
        rows_downloaded=len(df_co),
        message=(
            f"Downloaded cleared orders: {len(df_co):,} rows "
            f"(range={from_dt.date()}->{to_dt.date()}, chunks={len(chunks)})."
        ),
        df_co=df_co,
        from_utc=from_dt,
        to_utc=to_dt,
    )


def fetch_cleared_orders_df(
    *,
    betfair: dict[str, Any],
    lookback_days: int,
    page_size: int = 200,
) -> DownloadResult:
    """
    Legacy GUI/notebook entry point: download the last ``lookback_days`` of
    settled orders using interactive login.

    Preserved with an identical public signature so the GUI pipeline
    (``pipeline.run_pipeline``) continues to work unchanged. Internally
    delegates to :func:`fetch_cleared_orders_df_range` for the actual work;
    the chunking is a transparent robustness improvement.
    """
    utc_now = datetime.now(timezone.utc)
    from_dt = utc_now - timedelta(days=int(lookback_days))

    result = fetch_cleared_orders_df_range(
        betfair=betfair,
        from_date=from_dt,
        to_date=utc_now,
        page_size=page_size,
    )

    # Preserve the exact legacy message format the GUI currently displays.
    if result.attempted and result.df_co is not None:
        result.message = (
            f"Downloaded cleared orders: {len(result.df_co):,} rows "
            f"(lookback_days={lookback_days})."
        )
    return result


# -----------------------------
# Enrichment: market catalogue
# -----------------------------


def _chunked(seq: Iterable[str], n: int) -> Iterable[list[str]]:
    seq = list(seq)
    for i in range(0, len(seq), n):
        yield seq[i : i + n]


def enrich_with_market_catalogue(
    *,
    df_co: pd.DataFrame,
    betfair: dict[str, Any],
    cache_dir: Path,
    enable: bool = True,
    use_cache: bool = True,
    batch_size: int = 50,
    sleep_seconds: float = 0.20,
    status_cb: Optional[callable] = None,
    api_client: Optional[betfairlightweight.APIClient] = None,
) -> tuple[pd.DataFrame, EnrichResult]:
    """
    Notebook Cell 3, ported:
    - list_market_catalogue by marketId batches
    - cache at <cache_dir>/market_catalogue_event_cache.csv
    - deterministic column names: mkt_*, evt_*
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

    total_batches = (
        (len(missing_market_ids) + batch_size - 1) // batch_size
        if batch_size > 0
        else 0
    )
    for idx, batch in enumerate(_chunked(missing_market_ids, batch_size), start=1):
        if total_batches > 0:
            say(
                f"Enrichment: fetching batch {idx}/{total_batches} (batch_size={len(batch):,})..."
            )
        time.sleep(sleep_seconds)

        cats = trading.betting.list_market_catalogue(
            filter=filters.market_filter(market_ids=batch),
            max_results=1000,
            market_projection=["MARKET_START_TIME", "EVENT"],
        )
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
        df_out = df_work.merge(df_new_cache, on="marketId", how="left")

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

    return df_work, EnrichResult(
        attempted=True,
        markets_requested=len(missing_market_ids),
        markets_returned=returned_total,
        message="No enrichment rows available (cache empty and fetch returned none).",
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


def prune_snapshot_files(
    results_csv_dir: Path,
    keep: int = 14,
    status_cb: Optional[callable] = None,
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
    status_cb: Optional[callable] = None,
) -> pd.DataFrame:
    """
    Move rows settled more than ``archive_months`` ago into yearly compressed
    archives (``cleared_orders_archive_YYYY.csv.gz``) and return the trimmed
    canonical dataframe.

    Rows with an unparseable ``settledDate`` are always kept in the canonical.
    Archives are deduplicated on append, so re-running after a partial failure
    is safe. ``archive_months <= 0`` disables archival.
    """
    if archive_months <= 0 or df_canonical.empty or "settledDate" not in df_canonical.columns:
        return df_canonical

    settled = pd.to_datetime(df_canonical["settledDate"], utc=True, errors="coerce")
    cutoff = pd.Timestamp(datetime.now(timezone.utc)) - pd.DateOffset(months=archive_months)
    old_mask = settled.notna() & (settled < cutoff)
    if not old_mask.any():
        return df_canonical

    for year in sorted(settled[old_mask].dt.year.unique()):
        year_mask = old_mask & (settled.dt.year == year)
        chunk = df_canonical[year_mask]
        archive_path = results_csv_dir / f"cleared_orders_archive_{year}.csv.gz"
        if archive_path.exists():
            existing = pd.read_csv(archive_path, low_memory=False)
            cols = sorted(set(existing.columns).union(set(chunk.columns)))
            chunk = pd.concat(
                [existing.reindex(columns=cols), chunk.reindex(columns=cols)],
                ignore_index=True,
            )
        chunk = clean_and_remove_duplicates(chunk, status_cb=status_cb)
        tmp_path = archive_path.with_name(archive_path.name + ".tmp")
        chunk.to_csv(tmp_path, index=False, compression="gzip")
        tmp_path.replace(archive_path)
        if status_cb:
            status_cb(
                f"Archive: moved {int(year_mask.sum()):,} rows settled before "
                f"{cutoff.date()} into {archive_path.name} (now {len(chunk):,} rows)."
            )

    return df_canonical[~old_mask].reset_index(drop=True)


def write_csv_outputs(
    *,
    df_co: pd.DataFrame,
    results_csv_dir: Path,
    status_cb: Optional[callable] = None,
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

    update_csv_with_new_data(canonical_path, df_co, status_cb=status_cb)

    df_canonical = pd.read_csv(canonical_path)
    df_trimmed = archive_old_canonical_rows(
        df_canonical, results_csv_dir, archive_months=archive_months, status_cb=status_cb
    )
    if len(df_trimmed) < len(df_canonical):
        tmp_path = canonical_path.with_suffix(canonical_path.suffix + ".tmp")
        df_trimmed.to_csv(tmp_path, index=False)
        tmp_path.replace(canonical_path)

    df_trimmed.to_csv(snapshot_path, index=False)

    prune_snapshot_files(results_csv_dir, keep=snapshot_retention, status_cb=status_cb)

    return CsvWriteResult(
        canonical_path=canonical_path,
        snapshot_path=snapshot_path,
        rows_in_canonical=len(df_trimmed),
        message=f"Wrote canonical + snapshot CSV. canonical_rows={len(df_trimmed):,}.",
    )


# -----------------------------
# Azure prep (filter + aggregate + rows_to_write)
# -----------------------------


def _money2(x: Any) -> Decimal:
    return Decimal(str(x)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def prepare_azure_dataset(
    *,
    df_co: pd.DataFrame,
    allowed_event_type_ids: set[int] = {7, 4339},
) -> AzurePrepResult:
    """
    Notebook Cells 9–10, ported:
    - filter df_co to allowed eventTypeIds (numeric coercion)
    - aggregate by marketId (sum profit, count betId, min/max placedDate)
    - build rows_to_write list: (MarketID decimal, Profit decimal(2), Notes)
    """
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
    if df_azure_upload.empty:
        return AzurePrepResult(
            attempted=True,
            rows_after_filter=0,
            markets_aggregated=0,
            message="Azure prep produced 0 rows after filtering to allowed eventTypeIds.",
            df_market_results=None,
            rows_to_write=None,
        )

    df_market_results = df_azure_upload.groupby("marketId", as_index=False).agg(
        Profit=("profit", "sum"),
        Bets=("betId", "count"),
        FirstPlaced=("placedDate", "min"),
        LastPlaced=("placedDate", "max"),
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

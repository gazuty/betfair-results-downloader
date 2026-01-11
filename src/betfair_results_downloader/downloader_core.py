from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone, date
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Iterable, List, Optional, Tuple

import json
import time

import pandas as pd
import pytz

import betfairlightweight
from betfairlightweight import filters
from betfairlightweight.exceptions import APIError

from .csv_utils import update_csv_with_new_data


# -----------------------------
# Small results containers
# -----------------------------

@dataclass
class DownloadResult:
    attempted: bool
    rows_downloaded: int
    message: str
    df_co: Optional[pd.DataFrame] = None


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
            )
        except APIError as e:
            msg = str(e)
            is_timeout = ("TIMEOUT_ERROR" in msg) or ("ANGX-0010" in msg)
            if (not is_timeout) or attempt == max_retries:
                raise
            sleep_s = min(2 ** attempt, 20)
            time.sleep(sleep_s)


def fetch_cleared_orders_df(
    *,
    betfair: dict[str, Any],
    lookback_days: int,
    page_size: int = 200,
) -> DownloadResult:
    """
    Notebook Cell 2, ported:
    - login_interactive (no certs)
    - list_cleared_orders paginated
    - normalize schema and add Win + Sydney timezone columns
    """
    username = (betfair.get("username") or "").strip()
    password = betfair.get("password") or ""
    app_key = (betfair.get("app_key") or "").strip()

    if not username or not password or not app_key:
        return DownloadResult(
            attempted=False,
            rows_downloaded=0,
            message="Betfair credentials missing (username/password/app_key).",
            df_co=None,
        )

    utc_now = datetime.now(timezone.utc)
    from_dt = (utc_now - timedelta(days=int(lookback_days))).strftime("%Y-%m-%dT%H:%M:%SZ")
    to_dt = utc_now.strftime("%Y-%m-%dT%H:%M:%SZ")
    settled_range = betfairlightweight.filters.time_range(from_=from_dt, to=to_dt)

    trading = betfairlightweight.APIClient(
        username=username,
        password=password,
        app_key=app_key,
    )
    # Interactive login: opens browser / prompts; consistent with your notebook
    trading.login_interactive()

    indexrecord = 0
    all_rows: list[dict[str, Any]] = []

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

    df_co = pd.DataFrame(all_rows)

    required_cols = [
        "eventTypeId", "eventId", "marketId", "selectionId", "handicap", "betId",
        "placedDate", "persistenceType", "orderType", "side", "betOutcome",
        "priceRequested", "settledDate", "lastMatchedDate", "betCount",
        "priceMatched", "priceReduced", "sizeSettled", "profit",
        "customerOrderRef", "customerStrategyRef",
    ]
    for c in required_cols:
        if c not in df_co.columns:
            df_co[c] = pd.NA
    df_co = df_co[required_cols]

    def determine_win(row: pd.Series) -> int:
        if (row["side"] == "BACK" and row["betOutcome"] == "LOST") or (row["side"] == "LAY" and row["betOutcome"] == "WON"):
            return 0
        return 1

    if not df_co.empty:
        df_co["Win"] = df_co.apply(determine_win, axis=1)
    else:
        df_co["Win"] = pd.Series(dtype="int")

    # placedDate => Australia/Sydney
    df_co["placedDate"] = pd.to_datetime(df_co["placedDate"], utc=True, errors="coerce")
    aet_zone = pytz.timezone("Australia/Sydney")
    df_co["placedDate"] = df_co["placedDate"].dt.tz_convert(aet_zone)
    df_co["placedDateOnly"] = df_co["placedDate"].dt.date
    df_co["placedTimeOnly"] = df_co["placedDate"].dt.time

    return DownloadResult(
        attempted=True,
        rows_downloaded=len(df_co),
        message=f"Downloaded cleared orders: {len(df_co):,} rows (lookback_days={lookback_days}).",
        df_co=df_co,
    )


# -----------------------------
# Enrichment: market catalogue
# -----------------------------

def _chunked(seq: Iterable[str], n: int) -> Iterable[list[str]]:
    seq = list(seq)
    for i in range(0, len(seq), n):
        yield seq[i:i+n]


def enrich_with_market_catalogue(
    *,
    df_co: pd.DataFrame,
    betfair: dict[str, Any],
    repo_root: Path,
    enable: bool = True,
    use_cache: bool = True,
    batch_size: int = 50,
    sleep_seconds: float = 0.20,
    status_cb: Optional[callable] = None,
) -> tuple[pd.DataFrame, EnrichResult]:
    """
    Notebook Cell 3, ported:
    - list_market_catalogue by marketId batches
    - cache at outputs/market_catalogue_event_cache.csv
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

    if not username or not password or not app_key:
        return df_co, EnrichResult(
            attempted=False,
            markets_requested=0,
            markets_returned=0,
            message="Enrichment skipped (Betfair creds missing).",
            unique_market_ids=int(df_co["marketId"].nunique()) if "marketId" in df_co.columns else 0,
            use_cache=use_cache,
            batch_size=batch_size,
        )

    trading = betfairlightweight.APIClient(username=username, password=password, app_key=app_key)
    trading.login_interactive()

    out_dir = repo_root / "outputs"
    out_dir.mkdir(parents=True, exist_ok=True)
    cache_path = out_dir / "market_catalogue_event_cache.csv"
    snapshot_path = out_dir / "market_catalogue_event_latest.csv"

    df_work = df_co.copy()
    df_work["marketId"] = df_work["marketId"].astype(str)

    if use_cache and cache_path.exists():
        df_cache = pd.read_csv(cache_path, dtype=str)
    else:
        df_cache = pd.DataFrame()

    unique_market_ids = sorted(df_work["marketId"].dropna().unique().tolist())
    cached_market_ids = set(df_cache["marketId"].unique()) if (not df_cache.empty and "marketId" in df_cache.columns) else set()
    missing_market_ids = [m for m in unique_market_ids if m not in cached_market_ids]

    cache_hits = len(unique_market_ids) - len(missing_market_ids)
    cache_misses = len(missing_market_ids)

    say(f"Enrichment: unique_market_ids={len(unique_market_ids):,}, cache_hits={cache_hits:,}, cache_misses={cache_misses:,}.")

    fetched_rows: list[dict[str, Any]] = []
    returned_total = 0

    total_batches = (len(missing_market_ids) + batch_size - 1) // batch_size if batch_size > 0 else 0
    for idx, batch in enumerate(_chunked(missing_market_ids, batch_size), start=1):
        if total_batches > 0:
            say(f"Enrichment: fetching batch {idx}/{total_batches} (batch_size={len(batch):,})…")
        time.sleep(sleep_seconds)

        cats = trading.betting.list_market_catalogue(
            filter=filters.market_filter(market_ids=batch),
            max_results=1000,
            market_projection=["MARKET_START_TIME", "EVENT"],
        )
        returned_total += len(cats)

        for cat in cats:
            fetched_rows.append({
                "marketId": str(cat.market_id),
                "mkt_marketName": getattr(cat, "market_name", None),
                "mkt_marketStartTime": getattr(cat, "market_start_time", None),
                "evt_eventId": str(cat.event.id) if getattr(cat, "event", None) else None,
                "evt_eventName": cat.event.name if getattr(cat, "event", None) else None,
                "evt_countryCode": cat.event.country_code if getattr(cat, "event", None) else None,
            })

    df_fetched = pd.DataFrame(fetched_rows)

    if not df_fetched.empty:
        df_new_cache = pd.concat([df_cache, df_fetched], ignore_index=True) if not df_cache.empty else df_fetched
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
            msg = f"Enriched metadata using market catalogue. Cache rows={cache_rows:,}."

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

def write_csv_outputs(
    *,
    df_co: pd.DataFrame,
    results_csv_dir: Path,
) -> CsvWriteResult:
    """
    Notebook Cells 7–8, ported:
    - canonical: cleared_orders_cleaned.csv (idempotent update)
    - snapshot: cleared_orders_cleaned_YYYY-MM-DD.csv (copy of canonical)
    """
    results_csv_dir.mkdir(parents=True, exist_ok=True)

    canonical_path = results_csv_dir / "cleared_orders_cleaned.csv"
    today_str = date.today().isoformat()
    snapshot_path = results_csv_dir / f"cleared_orders_cleaned_{today_str}.csv"

    update_csv_with_new_data(canonical_path, df_co)

    df_canonical = pd.read_csv(canonical_path)
    df_canonical.to_csv(snapshot_path, index=False)

    return CsvWriteResult(
        canonical_path=canonical_path,
        snapshot_path=snapshot_path,
        rows_in_canonical=len(df_canonical),
        message=f"Wrote canonical + snapshot CSV. canonical_rows={len(df_canonical):,}.",
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
        return AzurePrepResult(False, 0, 0, f"Azure prep failed: missing columns {sorted(missing)}.")

    df_stage = df_co.copy()
    df_stage["eventTypeId"] = pd.to_numeric(df_stage["eventTypeId"], errors="coerce").astype("Int64")

    df_azure_upload = df_stage[df_stage["eventTypeId"].isin(list(allowed_event_type_ids))].copy()
    if df_azure_upload.empty:
        return AzurePrepResult(
            attempted=True,
            rows_after_filter=0,
            markets_aggregated=0,
            message="Azure prep produced 0 rows after filtering to allowed eventTypeIds.",
            df_market_results=None,
            rows_to_write=None,
        )

    df_market_results = (
        df_azure_upload
        .groupby("marketId", as_index=False)
        .agg(
            Profit=("profit", "sum"),
            Bets=("betId", "count"),
            FirstPlaced=("placedDate", "min"),
            LastPlaced=("placedDate", "max"),
        )
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

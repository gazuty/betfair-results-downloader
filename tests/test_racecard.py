"""Racecard capture: slicing under the page cap, upserts and pruning."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pandas as pd
import pytest

from betfair_results_downloader.racecard import (
    MAX_RESULTS,
    RACECARD_COLUMNS,
    catalogue_to_row,
    fetch_racecard,
    load_racecard,
    merge_racecard,
    prune_racecard,
    resolve_racecard_path,
    save_racecard,
    update_racecard,
)

NOW = datetime(2026, 11, 14, 12, 0, tzinfo=timezone.utc)


def _cat(market_id: str, event_name: str = "Morphettville (AUS) 14th Nov", **kw):
    return SimpleNamespace(
        market_id=market_id,
        market_name=kw.get("market_name", "R7 1200m Hcp"),
        market_start_time=kw.get("start", datetime(2026, 11, 14, 7, 0)),
        event=SimpleNamespace(
            id="35000001",
            name=event_name,
            venue="Morphettville",
            country_code="AU",
        ),
        event_type=SimpleNamespace(id="7", name="Horse Racing"),
    )


def test_catalogue_row_keeps_the_website_names() -> None:
    row = catalogue_to_row(_cat("1.265396576"), "2026-11-14T06:00:00Z")
    assert row == {
        "marketId": "1.265396576",
        "eventTypeId": "7",
        "eventId": "35000001",
        "eventName": "Morphettville (AUS) 14th Nov",
        "venue": "Morphettville",
        "countryCode": "AU",
        "marketName": "R7 1200m Hcp",
        "marketStartTime": "2026-11-14T07:00:00Z",
        "capturedUtc": "2026-11-14T06:00:00Z",
    }


def test_a_catalogue_without_an_event_name_is_skipped() -> None:
    assert catalogue_to_row(_cat("1.1", event_name=""), "x") is None


def test_a_full_slice_is_halved_until_it_fits() -> None:
    client = MagicMock()
    calls: list[tuple[str, str]] = []

    def respond(*, filter, max_results, market_projection):
        window = filter["marketStartTime"]
        calls.append((window["from"], window["to"]))
        start = datetime.fromisoformat(window["from"].replace("Z", "+00:00"))
        end = datetime.fromisoformat(window["to"].replace("Z", "+00:00"))
        # A dense card: anything wider than an hour overflows a page.
        n = MAX_RESULTS if end - start > timedelta(hours=1) else 10
        return [_cat(f"1.{start.timestamp():.0f}{i}") for i in range(n)]

    client.betting.list_market_catalogue.side_effect = respond
    cats, truncated = fetch_racecard(
        client, now=NOW, horizon_hours=2, lookback_hours=0, slice_hours=2
    )

    assert truncated == 0
    assert len(cats) == 20  # two one-hour halves of ten markets
    assert calls[0] == ("2026-11-14T12:00:00Z", "2026-11-14T14:00:00Z")


def test_a_slice_still_full_at_the_minimum_width_is_reported_truncated() -> None:
    client = MagicMock()
    client.betting.list_market_catalogue.return_value = [_cat("1.1")] * MAX_RESULTS
    _cats, truncated = fetch_racecard(
        client, now=NOW, horizon_hours=1, lookback_hours=0, slice_hours=1
    )
    # 1h -> 30m -> 15m slices: four at the floor, each still full.
    assert truncated == 4


def test_merge_upserts_by_numeric_market_id() -> None:
    existing = pd.DataFrame(
        [
            dict.fromkeys(RACECARD_COLUMNS, "")
            | {"marketId": "1.25194156", "eventName": "old"}
        ]
    )
    merged = merge_racecard(
        existing,
        [
            dict.fromkeys(RACECARD_COLUMNS, "")
            | {"marketId": "1.251941560", "eventName": "new"}
        ],
    )
    assert merged["eventName"].tolist() == ["new"]


def test_prune_keeps_bet_markets_and_recent_ones() -> None:
    def row(mid: str, start: str) -> dict:
        return dict.fromkeys(RACECARD_COLUMNS, "") | {
            "marketId": mid,
            "marketStartTime": start,
        }

    df = pd.DataFrame(
        [
            row("1.1", "2026-11-01T00:00:00Z"),  # old, bet on: kept
            row("1.2", "2026-11-01T00:00:00Z"),  # old, not bet on: dropped
            row("1.3", "2026-11-13T00:00:00Z"),  # recent: kept
            row("1.4", ""),  # unparseable: kept
        ]
    )
    kept = prune_racecard(df, bet_market_keys={"1.1"}, now=NOW)
    assert kept["marketId"].tolist() == ["1.1", "1.3", "1.4"]


def test_update_writes_the_store_and_leaves_it_alone_on_failure(tmp_path: Path) -> None:
    client = MagicMock()
    client.betting.list_market_catalogue.return_value = [_cat("1.265396576")]
    result = update_racecard(
        client=client, cache_dir=tmp_path, bet_market_keys=set(), now=NOW
    )
    assert result.captured >= 1
    stored = load_racecard(resolve_racecard_path(tmp_path))
    assert stored["marketId"].tolist() == ["1.265396576"]
    before = resolve_racecard_path(tmp_path).read_bytes()

    client.betting.list_market_catalogue.side_effect = RuntimeError("down")
    with pytest.raises(RuntimeError):
        update_racecard(
            client=client, cache_dir=tmp_path, bet_market_keys=set(), now=NOW
        )
    assert resolve_racecard_path(tmp_path).read_bytes() == before


def test_the_store_round_trips_market_ids_as_strings(tmp_path: Path) -> None:
    path = resolve_racecard_path(tmp_path)
    df = pd.DataFrame(
        [dict.fromkeys(RACECARD_COLUMNS, "") | {"marketId": "1.251500100"}]
    )
    save_racecard(df, path)
    assert load_racecard(path)["marketId"].tolist() == ["1.251500100"]


def test_concurrent_updates_both_land_in_the_store(tmp_path: Path) -> None:
    """
    The hourly job and a scheduled run overlapping: the second save must
    merge into the first's result, not replace it with a stale merge.
    """
    import threading

    from betfair_results_downloader import racecard as rc

    first_loaded = threading.Event()
    real_load = rc.load_racecard

    def slow_first_load(path):
        df = real_load(path)
        if threading.current_thread().name == "first":
            first_loaded.set()
            # Hold the store open long enough for the second update to try.
            threading.Event().wait(0.3)
        return df

    def run(name: str, market_id: str) -> None:
        client = MagicMock()
        client.betting.list_market_catalogue.return_value = [_cat(market_id)]
        update_racecard(
            client=client, cache_dir=tmp_path, bet_market_keys=set(), now=NOW
        )

    rc.load_racecard = slow_first_load
    try:
        first = threading.Thread(target=run, args=("first", "1.111"), name="first")
        second = threading.Thread(target=run, args=("second", "1.222"), name="second")
        first.start()
        first_loaded.wait(2)
        second.start()
        first.join(5)
        second.join(5)
    finally:
        rc.load_racecard = real_load

    stored = load_racecard(resolve_racecard_path(tmp_path))
    assert set(stored["marketId"]) == {"1.111", "1.222"}
    assert not list(tmp_path.glob("*.tmp"))


def test_the_module_loads_and_updates_without_posix_locking(
    tmp_path: Path, monkeypatch
) -> None:
    """Windows has no fcntl: the import must not fail, and an update still saves."""
    from betfair_results_downloader import racecard as rc

    monkeypatch.setattr(rc, "fcntl", None)
    monkeypatch.setattr(rc, "msvcrt", None)
    client = MagicMock()
    client.betting.list_market_catalogue.return_value = [_cat("1.265396576")]
    rc.update_racecard(
        client=client, cache_dir=tmp_path, bet_market_keys=set(), now=NOW
    )
    assert load_racecard(resolve_racecard_path(tmp_path))["marketId"].tolist() == [
        "1.265396576"
    ]


def test_importing_the_feed_does_not_need_fcntl(monkeypatch) -> None:
    import builtins
    import importlib
    import sys

    real_import = builtins.__import__

    def no_fcntl(name, *args, **kwargs):
        if name == "fcntl":
            raise ImportError("No module named 'fcntl'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_fcntl)
    for mod in (
        "betfair_results_downloader.racecard",
        "betfair_results_downloader.market_results",
    ):
        monkeypatch.delitem(sys.modules, mod, raising=False)
    racecard = importlib.import_module("betfair_results_downloader.racecard")
    importlib.import_module("betfair_results_downloader.market_results")
    assert racecard.fcntl is None

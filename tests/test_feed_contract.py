"""
The results feed as a contract with betfair-dashboard-py.

The dashboard reads ``market_results.csv`` and ``market_results.meta.json``
and has its own copy of the two files in ``tests/fixtures/feed_contract/``
(its ``tests/fixtures/downloader_feed/``), which its tests load, validate
and merge. This test builds the feed from fixed inputs and requires the
result to equal those files byte for byte, so any change to the feed's
columns, formats, names or meta keys fails here first.

When a change is intended:

1. regenerate the files: ``python tests/test_feed_contract.py``;
2. copy both to betfair-dashboard-py ``tests/fixtures/downloader_feed/`` and
   make its tests pass in the same piece of work;
3. bump ``SCHEMA_VERSION`` if a column was removed or changed meaning.

Every value is invented; no real market, bet or amount appears here.
"""

from __future__ import annotations

import json
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from betfair_results_downloader.commission import COMMISSION_COLUMNS
from betfair_results_downloader.market_results import (
    FEED_COLUMNS,
    FEED_FILENAME,
    META_FILENAME,
    build_market_results,
    write_market_results,
)
from betfair_results_downloader.market_status import STATUS_COLUMNS
from betfair_results_downloader.racecard import RACECARD_COLUMNS

FIXTURES = Path(__file__).parent / "fixtures" / "feed_contract"
NOW = datetime(2026, 11, 14, 12, 0, tzinfo=timezone.utc)
DOWNLOADED = datetime(2026, 11, 14, 11, 58, tzinfo=timezone.utc)

# What the dashboard reads from the meta. Other keys are diagnostics.
META_KEYS_THE_DASHBOARD_READS = {
    "schemaVersion",
    "generatedUtc",
    "lastDownloadUtc",
    "csvSha256",
    "dataThroughUtc",
    "markets",
    "heldPendingMarkets",
    "heldStatusUncheckedMarkets",
    "heldCommissionUnknownMarkets",
    "nameSources",
}


def _bets() -> pd.DataFrame:
    rows = [
        # A race named from the racecard. The canonical holds the id as a
        # float once wrote it; the feed must spell it as Betfair does.
        (
            "101",
            "1.2515001",
            "7",
            "2026-11-14T07:04:30.000Z",
            "20.00",
            "18:01 Morphettville",
            "Win",
        ),
        (
            "102",
            "1.2515001",
            "7",
            "2026-11-14T07:04:37.000Z",
            "-8.45",
            "18:01 Morphettville",
            "Win",
        ),
        # A race the racecard missed at a venue never seen: country Unknown.
        (
            "103",
            "1.265000002",
            "4339",
            "2026-11-14T08:01:10.000Z",
            "-4.00",
            "19:01 Romford",
            "Win",
        ),
        # A sport, named from itemDescription.
        (
            "104",
            "1.265000003",
            "1",
            "2026-11-13T12:37:43.000Z",
            "44.00",
            "Bochum v SV Darmstadt",
            "Over/Under 0.5 Goals",
        ),
        # An outright seen partially settled, closed by its last leg.
        (
            "105",
            "1.265000004",
            "2",
            "2026-11-08T03:10:00.000Z",
            "-6.00",
            "Example Open 2026",
            "Winner",
        ),
        (
            "106",
            "1.265000004",
            "2",
            "2026-11-13T05:53:59.000Z",
            "30.13",
            "Example Open 2026",
            "Winner",
        ),
        # A win too small to be charged: a genuine zero commission.
        (
            "107",
            "1.265000005",
            "7",
            "2026-11-14T09:15:00.000Z",
            "0.07",
            "20:10 Ascot",
            "Win",
        ),
        # Held back: still partially settled.
        (
            "108",
            "1.265000006",
            "3",
            "2026-11-12T22:00:00.000Z",
            "12.00",
            "Example Masters 2026",
            "Winner",
        ),
        # Held back: commission not yet read.
        (
            "109",
            "1.265000007",
            "7",
            "2026-11-14T10:30:00.000Z",
            "9.00",
            "21:25 Newcastle",
            "Win",
        ),
    ]
    return pd.DataFrame(
        rows,
        columns=[
            "betId",
            "marketId",
            "eventTypeId",
            "settledDate",
            "profit",
            "evt_eventName",
            "mkt_marketName",
        ],
    )


def _store() -> pd.DataFrame:
    read = "2026-11-14T11:58:00Z"
    rows = [
        ("1.251500100", "7", "2026-11-14T07:04:37.000Z", "2", "11.55", "0.63", read),
        ("1.265000002", "4339", "2026-11-14T08:01:10.000Z", "1", "-4.00", "0.00", read),
        ("1.265000003", "1", "2026-11-13T12:37:43.000Z", "1", "44.00", "2.20", read),
        ("1.265000004", "2", "2026-11-13T05:53:59.000Z", "2", "24.13", "1.30", read),
        ("1.265000005", "7", "2026-11-14T09:15:00.000Z", "1", "0.07", "0.00", read),
        ("1.265000006", "3", "2026-11-12T22:00:00.000Z", "1", "12.00", "0.00", read),
    ]
    return pd.DataFrame(rows, columns=COMMISSION_COLUMNS)


def _status() -> pd.DataFrame:
    def row(
        market_id,
        status,
        first_pending="",
        closed="2026-11-14T11:57:00Z",
        last_pending="",
    ):
        return {
            "marketId": market_id,
            "status": status,
            "activeRunners": "0",
            "source": "book",
            "checkedUtc": "2026-11-14T11:57:00Z",
            "firstPendingUtc": first_pending,
            "closedObservedUtc": closed,
            "lastPendingUtc": last_pending,
        }

    rows = [
        row("1.251500100", "CLOSED"),
        row("1.265000002", "CLOSED"),
        row("1.265000003", "CLOSED"),
        row(
            "1.265000004",
            "CLOSED",
            "2026-11-08T09:00:00Z",
            "2026-11-13T09:00:00Z",
            "2026-11-12T23:00:00Z",
        ),
        row("1.265000005", "CLOSED"),
        row("1.265000006", "OPEN", "2026-11-12T23:00:00Z", "", "2026-11-14T11:57:00Z"),
        row("1.265000007", "CLOSED"),
    ]
    return pd.DataFrame(rows, columns=STATUS_COLUMNS)


def _racecard() -> pd.DataFrame:
    row = dict.fromkeys(RACECARD_COLUMNS, "") | {
        "marketId": "1.251500100",
        "eventTypeId": "7",
        "eventName": "Morphettville (AUS) 14th Nov",
        "venue": "Morphettville",
        "countryCode": "AU",
        "marketName": "R7 1200m Hcp",
        "marketStartTime": "2026-11-14T07:01:00Z",
        "capturedUtc": "2026-11-14T06:30:00Z",
    }
    return pd.DataFrame([row], columns=RACECARD_COLUMNS)


def build_contract_feed(directory: Path) -> None:
    """Write the contract feed to ``directory`` the way a run writes it."""
    result = build_market_results(
        _bets(),
        market_status=_status(),
        market_commission=_store(),
        racecard=_racecard(),
        catalogue=None,
        now=NOW,
    )
    write_market_results(directory, result, downloaded_at=DOWNLOADED)


def test_the_feed_built_from_fixed_inputs_is_the_shared_fixture(tmp_path) -> None:
    build_contract_feed(tmp_path)
    for name in (FEED_FILENAME, META_FILENAME):
        assert (tmp_path / name).read_bytes() == (FIXTURES / name).read_bytes(), (
            f"{name} no longer matches tests/fixtures/feed_contract/{name}. If the "
            f"change is intended, follow the steps at the top of this file: the "
            f"dashboard holds a copy of these files and must change with them."
        )


def test_the_fixture_has_the_columns_and_meta_the_dashboard_reads() -> None:
    feed = pd.read_csv(FIXTURES / FEED_FILENAME, dtype=str, keep_default_na=False)
    assert (
        list(feed.columns)
        == FEED_COLUMNS
        == [
            "marketId",
            "eventTypeId",
            "sport",
            "market",
            "nameSource",
            "settledUtc",
            "settledLocal",
            "betCount",
            "grossProfit",
            "commission",
            "netProfit",
        ]
    )
    meta = json.loads((FIXTURES / META_FILENAME).read_text())
    assert META_KEYS_THE_DASHBOARD_READS <= set(meta)
    assert meta["lastDownloadUtc"] == "2026-11-14T11:58:00Z"
    assert (meta["heldPendingMarkets"], meta["heldCommissionUnknownMarkets"]) == (1, 1)


def test_the_fixture_covers_each_shape_the_dashboard_must_parse() -> None:
    feed = pd.read_csv(FIXTURES / FEED_FILENAME, dtype=str, keep_default_na=False)
    by_id = feed.set_index("marketId")
    # The float-damaged canonical id comes out as Betfair spells it.
    assert "1.251500100" in by_id.index and "1.2515001" not in by_id.index
    assert by_id.loc["1.251500100", "market"] == (
        "Horse Racing / Morphettville (AUS) 14th Nov : R7 1200m Hcp"
    )
    assert by_id.loc["1.265000002", "market"] == (
        "Greyhound Racing / Romford (Unknown) 14th Nov : Win"
    )
    assert set(feed["nameSource"]) == {"racecard", "fallback", "item_description"}
    # The outright is dated at its last leg, in Sydney time.
    assert by_id.loc["1.265000004", "settledUtc"] == "2026-11-13T05:53:59Z"
    assert by_id.loc["1.265000004", "settledLocal"] == "2026-11-13 16:53:59"
    # Held-back markets are absent, not present with a zero.
    assert not {"1.265000006", "1.265000007"} & set(by_id.index)
    assert by_id.loc["1.265000005", "commission"] == "0.0"


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as scratch:
        build_contract_feed(Path(scratch))
        FIXTURES.mkdir(parents=True, exist_ok=True)
        for name in (FEED_FILENAME, META_FILENAME):
            (FIXTURES / name).write_bytes((Path(scratch) / name).read_bytes())
            sys.stdout.write(f"wrote {FIXTURES / name}\n")

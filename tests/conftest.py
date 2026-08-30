"""
Shared fixtures, and a realistic canonical.

The suite otherwise exercises the CSV pipeline at one to three rows with
three or four columns. The artefact the system actually protects is ~1M rows
across 39 mixed-dtype columns, and the dedupe/reindex/archive interaction
only bites at that shape -- so the fixtures here reproduce the real schema.
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

# The real canonical header, in file order.
CANONICAL_COLUMNS = [
    "Win",
    "betCount",
    "betId",
    "betOutcome",
    "comp_competitionId",
    "comp_competitionName",
    "customerOrderRef",
    "customerStrategyRef",
    "desc_bettingType",
    "desc_marketType",
    "desc_turnInPlayEnabled",
    "eventId",
    "eventTypeId",
    "evt_countryCode",
    "evt_eventId",
    "evt_eventName",
    "evt_openDate",
    "evt_timezone",
    "evt_venue",
    "handicap",
    "lastMatchedDate",
    "marketId",
    "market_type",
    "mkt_marketName",
    "mkt_marketStartTime",
    "orderType",
    "persistenceType",
    "placedDate",
    "placedDateOnly",
    "placedTimeOnly",
    "priceMatched",
    "priceReduced",
    "priceRequested",
    "profit",
    "runner_name",
    "selectionId",
    "settledDate",
    "side",
    "sizeSettled",
]

HORSES = 7
GREYHOUNDS = 4339


def _market_id(rng: random.Random) -> str:
    """
    Betfair market IDs are '1.' plus nine digits, and roughly one in ten ends
    in a zero -- which is exactly the digit a float round-trip destroys.
    """
    return "1." + "".join(str(rng.randint(0, 9)) for _ in range(9))


def make_cleared_orders(
    rows: int = 500,
    *,
    start: datetime | None = None,
    seed: int = 20260830,
    first_bet_id: int = 1,
) -> pd.DataFrame:
    """Build a frame with the real schema, dtypes and awkward values."""
    rng = random.Random(seed)
    start = start or datetime(2026, 6, 1, 0, 0, tzinfo=timezone.utc)

    records = []
    for i in range(rows):
        settled = start + timedelta(minutes=17 * i)
        placed = settled - timedelta(hours=2)
        event_type = HORSES if i % 3 else GREYHOUNDS
        records.append(
            {
                "Win": rng.choice([0, 1]),
                "betCount": rng.randint(1, 4),
                "betId": first_bet_id + i,
                "betOutcome": rng.choice(["WON", "LOST"]),
                "comp_competitionId": rng.choice(["", "12345678"]),
                "comp_competitionName": rng.choice(["", "Race Series"]),
                "customerOrderRef": "",
                "customerStrategyRef": rng.choice(["", "strat-a"]),
                "desc_bettingType": "ODDS",
                "desc_marketType": rng.choice(["WIN", "PLACE"]),
                "desc_turnInPlayEnabled": rng.choice([True, False]),
                "eventId": str(rng.randint(30000000, 39999999)),
                "eventTypeId": event_type,
                "evt_countryCode": rng.choice(["AU", "GB", ""]),
                "evt_eventId": str(rng.randint(30000000, 39999999)),
                "evt_eventName": rng.choice(["Randwick", "Ascot (AUS)", ""]),
                "evt_openDate": settled.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                "evt_timezone": "Australia/Sydney",
                "evt_venue": rng.choice(["Randwick", "", "The Meadows"]),
                "handicap": 0.0,
                "lastMatchedDate": settled.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                "marketId": _market_id(rng),
                "market_type": rng.choice(["WIN", "PLACE"]),
                "mkt_marketName": rng.choice(["R1 1200m", "", "R7 Final"]),
                "mkt_marketStartTime": settled.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                "orderType": "LIMIT",
                "persistenceType": "LAPSE",
                "placedDate": placed.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                "placedDateOnly": placed.date().isoformat(),
                "placedTimeOnly": placed.time().isoformat(),
                "priceMatched": round(rng.uniform(1.5, 20.0), 2),
                "priceReduced": rng.choice([True, False]),
                "priceRequested": round(rng.uniform(1.5, 20.0), 2),
                "profit": round(rng.uniform(-50, 50), 2),
                "runner_name": rng.choice(["Fast Horse", "5. Quick Dog", ""]),
                "selectionId": rng.randint(1000000, 99999999),
                "settledDate": settled.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                "side": rng.choice(["BACK", "LAY"]),
                "sizeSettled": round(rng.uniform(1, 100), 2),
            }
        )

    return pd.DataFrame.from_records(records, columns=CANONICAL_COLUMNS)


@pytest.fixture
def cleared_orders() -> pd.DataFrame:
    return make_cleared_orders()


@pytest.fixture
def results_dir(tmp_path):
    d = tmp_path / "Results Database"
    d.mkdir()
    return d

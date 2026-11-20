"""
Breakouts: a named subset of bets reported on its own lines.

A breakout is defined in ``credentials.json`` (``report.breakouts``), never
in code, so a personal staking rule stays out of the repository. It selects
bet rows by any of event type, side and order type, and the report shows its
bets, winners, gross, commission and net for each period.

Commission is an estimate. Betfair charges it per market on the net winnings
of every bet in the market, so a subset of bets has no commission of its
own. The figure here is the difference between what the market was charged
and what it would have been charged without the breakout's bets (see
:func:`estimated_commission`). It is negative when a losing bet reduced the
commission on the market's other winnings.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

import pandas as pd

MARKET_KEY = "_key"

# Below this a market's own commission rate is not read from it: commission
# is rounded to the cent, so on a small win the ratio is noise (0.00 charged
# on a 4 cent win is not a rate of zero). Such a market is priced at its
# sport's typical rate, and is left out of the sample that rate comes from.
RATE_MIN_GROSS = 1.0

# What a criterion is called in the configuration, and the canonical column
# it is matched against.
_TEXT_CRITERIA: dict[str, str] = {"side": "side", "order_type": "orderType"}
_EVENT_TYPE_COLUMN = "eventTypeId"
_KNOWN_KEYS = frozenset({"name", "event_type_id", *_TEXT_CRITERIA})


@dataclass(frozen=True)
class BreakoutSpec:
    """Which bet rows belong to a breakout. Unset criteria match every row."""

    name: str
    event_type_id: int | None = None
    side: str | None = None
    order_type: str | None = None

    def required_columns(self) -> tuple[str, ...]:
        columns = [
            column
            for key, column in _TEXT_CRITERIA.items()
            if getattr(self, key) is not None
        ]
        if self.event_type_id is not None:
            columns.append(_EVENT_TYPE_COLUMN)
        return tuple(columns)

    def mask(self, rows: pd.DataFrame) -> pd.Series:
        """True for the rows this breakout selects."""
        selected = pd.Series(True, index=rows.index)
        if self.event_type_id is not None:
            # The canonical is read with inferred dtypes, so the id can be an
            # int, a float or text depending on what else is in the column.
            ids = pd.to_numeric(rows[_EVENT_TYPE_COLUMN], errors="coerce")
            selected &= ids == self.event_type_id
        for key, column in _TEXT_CRITERIA.items():
            wanted = getattr(self, key)
            if wanted is not None:
                values = rows[column].fillna("").astype(str).str.strip().str.upper()
                selected &= values == wanted
        return selected


@dataclass(frozen=True)
class BreakoutFigures:
    """One period of one breakout."""

    label: str
    bets: int = 0
    winners: int = 0
    gross: float = 0.0
    # Estimated; see the module docstring.
    commission: float = 0.0
    # Markets in the period whose commission could not be estimated: the
    # market's charge is not known yet, or there was no rate to price a
    # saving with. They count as $0.00 and the line says so.
    unknown_markets: int = 0

    @property
    def net(self) -> float:
        return self.gross - self.commission


@dataclass(frozen=True)
class BreakoutReport:
    """A breakout's periods, or the reason it could not be reported."""

    name: str
    periods: tuple[BreakoutFigures, ...] = ()
    problem: str | None = None


def parse_breakouts(creds: Any) -> tuple[tuple[BreakoutSpec, ...], tuple[str, ...]]:
    """
    Read ``report.breakouts`` from the loaded credentials.

    Returns the usable definitions and, separately, a sentence for each one
    that was not usable. A mistake in one entry must not cost the whole
    report, and must not pass silently either: the report prints the
    sentences as warnings.
    """
    if not isinstance(creds, dict):
        return (), ()
    report = creds.get("report")
    if report is None:
        return (), ()
    if not isinstance(report, dict):
        return (), ("the 'report' setting is not an object",)
    raw = report.get("breakouts")
    if raw is None:
        return (), ()
    if not isinstance(raw, list):
        return (), ("'report.breakouts' is not a list",)

    specs: list[BreakoutSpec] = []
    problems: list[str] = []
    seen: set[str] = set()
    for position, entry in enumerate(raw, start=1):
        try:
            spec = _parse_entry(entry)
        except ValueError as exc:
            problems.append(f"breakout {position}: {exc}")
            continue
        if spec.name in seen:
            problems.append(f"breakout {position}: the name is used twice")
            continue
        seen.add(spec.name)
        specs.append(spec)
    return tuple(specs), tuple(problems)


def _parse_entry(entry: Any) -> BreakoutSpec:
    if not isinstance(entry, dict):
        raise ValueError("not an object")
    unknown = sorted(set(entry) - _KNOWN_KEYS)
    if unknown:
        # A misspelt criterion would otherwise be ignored and the breakout
        # would quietly report far more bets than intended.
        raise ValueError(f"unknown setting {unknown[0]!r}")
    name = entry.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ValueError("it has no name")

    event_type_id = entry.get("event_type_id")
    if event_type_id is not None:
        # bool is an int in Python; true is not an event type.
        if isinstance(event_type_id, bool) or not isinstance(event_type_id, int):
            raise ValueError("event_type_id is not a whole number")

    text: dict[str, str | None] = {}
    for key in _TEXT_CRITERIA:
        value = entry.get(key)
        if value is None:
            text[key] = None
        elif isinstance(value, str) and value.strip():
            text[key] = value.strip().upper()
        else:
            raise ValueError(f"{key} is not text")

    if event_type_id is None and not any(text.values()):
        raise ValueError("it selects every bet; give it at least one criterion")
    return BreakoutSpec(name=name.strip(), event_type_id=event_type_id, **text)


def typical_rates(market_totals: pd.DataFrame) -> dict[str, float]:
    """
    Per sport, the median commission rate over winning markets with a known
    charge. A sport with no such market has no entry.

    Used where a market cannot supply its own rate: it lost overall, so
    nothing was charged, or it won too little for the ratio to mean anything.
    """
    sample = market_totals.loc[
        ~market_totals["unknown"]
        & (market_totals["gross"] >= RATE_MIN_GROSS)
        & (market_totals["commission"] > 0)
    ]
    if sample.empty:
        return {}
    rates = sample["commission"] / sample["gross"]
    return {
        str(sport): float(rate)
        for sport, rate in rates.groupby(sample["sport"]).median().items()
    }


def estimated_commission(
    market_gross: float,
    market_commission: float,
    breakout_gross: float,
    typical_rate: float | None,
) -> float | None:
    """
    Commission the market paid because of the breakout's bets.

    It is the commission charged less the commission the market's other bets
    would have attracted alone. On a winning market the market's own rate is
    known, and the answer is the charge in proportion to the breakout's
    share of the winnings, capped at all of it. On a market that lost
    overall nothing was charged; if the other bets would have won without
    the breakout's losses, the breakout saved their commission, priced at
    ``typical_rate``. A market that won less than ``RATE_MIN_GROSS`` is
    priced the same way, since its own ratio is rounding noise.

    ``None`` when the other bets' commission needs the typical rate and the
    sport has none on file: the figure is then unknown, not zero.
    """
    others = market_gross - breakout_gross
    if others <= 0:
        # The other bets would have paid nothing alone, at any rate.
        return market_commission
    if market_gross >= RATE_MIN_GROSS:
        return market_commission - (market_commission / market_gross) * others
    if typical_rate is None:
        return None
    return market_commission - typical_rate * others


def _market_table(
    final: pd.DataFrame, markets: pd.DataFrame, selected: pd.Series
) -> pd.DataFrame:
    """
    One row per market the breakout bet in: when its commission lands, the
    estimated commission, and whether that estimate is unknown (the market's
    charge is not known yet, or no rate was available to price a saving).
    """
    bets = final.loc[selected]
    sports = set(bets["sport"].unique())
    totals = (
        final.loc[final["sport"].isin(sports)]
        .groupby(MARKET_KEY, sort=False)["profit"]
        .sum()
        .rename("gross")
        .reset_index()
        .merge(markets, on=MARKET_KEY, how="left")
    )
    totals["unknown"] = totals["unknown"].fillna(True).astype(bool)
    totals["commission"] = totals["commission"].fillna(0.0)
    rates = typical_rates(totals)

    own = bets.groupby(MARKET_KEY, sort=False)["profit"].sum().rename("own_gross")
    table = totals.merge(own, on=MARKET_KEY, how="inner")
    estimates: list[float] = []
    unknowns: list[bool] = []
    for gross, charged, own_gross, sport, unknown in zip(
        table["gross"],
        table["commission"],
        table["own_gross"],
        table["sport"],
        table["unknown"],
    ):
        estimate = (
            None
            if unknown
            else estimated_commission(gross, charged, own_gross, rates.get(sport))
        )
        estimates.append(0.0 if estimate is None else estimate)
        unknowns.append(estimate is None)
    table["estimate"] = estimates
    table["unknown"] = unknowns
    return table


def build_breakout(
    spec: BreakoutSpec,
    final: pd.DataFrame,
    markets: pd.DataFrame,
    windows: list[tuple[str, datetime, datetime | None]],
) -> BreakoutReport:
    """
    Figures for ``spec`` over each ``(label, start, end)`` window.

    ``final`` is the report's settled, non-pending bet rows carrying
    ``MARKET_KEY``; ``markets`` is ``market_commission_frame`` for the same
    rows. Gross, bets and winners follow each bet's own settled time and
    commission follows the market's, exactly as in the main sections, so a
    breakout that selected every bet would reproduce the Total line's gross.
    """
    missing = [c for c in spec.required_columns() if c not in final.columns]
    if missing:
        return BreakoutReport(
            name=spec.name, problem=f"the results have no '{missing[0]}' column"
        )

    selected = spec.mask(final)
    if not selected.any():
        return BreakoutReport(
            name=spec.name,
            periods=tuple(BreakoutFigures(label) for label, _start, _end in windows),
        )

    bets = final.loc[selected]
    table = _market_table(final, markets, selected)

    periods: list[BreakoutFigures] = []
    for label, start, end in windows:
        rows = bets.loc[bets["settled_dt_local"] >= start]
        charged = table.loc[table["last_settled_local"] >= start]
        if end is not None:
            rows = rows.loc[rows["settled_dt_local"] < end]
            charged = charged.loc[charged["last_settled_local"] < end]
        periods.append(
            BreakoutFigures(
                label=label,
                bets=len(rows),
                winners=int((rows["profit"] > 0).sum()),
                gross=float(rows["profit"].sum()),
                commission=float(charged["estimate"].sum()),
                unknown_markets=int(charged["unknown"].sum()),
            )
        )
    return BreakoutReport(name=spec.name, periods=tuple(periods))

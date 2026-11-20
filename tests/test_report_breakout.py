"""
Breakouts in the daily report: a configured subset of bets on its own lines.

Commission for a subset is an estimate (Betfair charges per market), so the
arithmetic is pinned case by case here, and the report is checked to keep
its account-wide sections exactly as they were.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from betfair_results_downloader.__main__ import main
from betfair_results_downloader.commission import COMMISSION_COLUMNS
from betfair_results_downloader.market_status import STATUS_COLUMNS
from betfair_results_downloader.reporting.breakout import (
    BreakoutFigures,
    BreakoutSpec,
    estimated_commission,
    parse_breakouts,
)
from betfair_results_downloader.reporting.daily_dm_report import (
    build_daily_dm_report_from_dataframe,
)

SYDNEY = ZoneInfo("Australia/Sydney")
# Saturday 6 June 2026, 9:00 PM Sydney. Week started Sunday 31 May; month
# started Monday 1 June.
REPORT_AT = datetime(2026, 6, 6, 21, 0, tzinfo=SYDNEY)
TODAY = "2026-06-06T02:00:00Z"  # Saturday noon, Sydney
YESTERDAY = "2026-06-05T02:00:00Z"
LAST_MONTH = "2026-05-20T02:00:00Z"

SUBSET = BreakoutSpec("Soccer lays", event_type_id=1, side="LAY", order_type="LIMIT")
OTHER_SPORT = 4339


def _bet(
    bet_id: str,
    market_id: str,
    profit: float,
    settled: str = TODAY,
    *,
    side: str = "BACK",
    order_type: str = "LIMIT",
    event_type: object = 1,
) -> dict:
    return {
        "betId": bet_id,
        "marketId": market_id,
        "eventTypeId": event_type,
        "profit": profit,
        "settledDate": settled,
        "side": side,
        "orderType": order_type,
    }


def _subset_bet(
    bet_id: str, market_id: str, profit: float, settled: str = TODAY
) -> dict:
    return _bet(bet_id, market_id, profit, settled, side="LAY")


def _commission(market_id: str, amount: str, settled: str = TODAY) -> dict:
    return {
        "marketId": market_id,
        "eventTypeId": "1",
        "settledDateUtc": settled,
        "betCount": "1",
        "grossProfit": "0.00",
        "commission": amount,
        "fetchedUtc": "2026-06-06T10:00:00Z",
    }


def _store(*rows: dict) -> pd.DataFrame:
    return pd.DataFrame(list(rows), columns=COMMISSION_COLUMNS, dtype=str)


def _report(bets: list[dict], store: pd.DataFrame, spec: BreakoutSpec = SUBSET):
    return build_daily_dm_report_from_dataframe(
        pd.DataFrame(bets),
        report_dt=REPORT_AT,
        market_commission=store,
        breakouts=(spec,),
    )


def _periods(report) -> dict[str, BreakoutFigures]:
    (breakout,) = report.breakouts
    assert breakout.problem is None
    return {period.label: period for period in breakout.periods}


# --- the commission estimate -------------------------------------------------


def test_a_winner_alone_in_its_market_carries_the_whole_charge() -> None:
    assert estimated_commission(200.0, 12.0, 200.0, 0.05) == pytest.approx(12.0)


def test_a_winner_carries_its_share_when_other_bets_won_too() -> None:
    # Market won 200 at 6%; the breakout won 150 of it.
    assert estimated_commission(200.0, 12.0, 150.0, 0.05) == pytest.approx(9.0)


def test_a_winner_carries_all_of_it_when_the_other_bets_lost() -> None:
    # Breakout won 250, the other bets lost 50: alone they paid nothing.
    assert estimated_commission(200.0, 12.0, 250.0, 0.05) == pytest.approx(12.0)


def test_a_loser_in_a_winning_market_saved_commission_at_that_markets_rate() -> None:
    # Other bets won 102, the breakout lost 2; the market paid 6% on 100.
    assert estimated_commission(100.0, 6.0, -2.0, 0.05) == pytest.approx(-0.12)


def test_a_loser_that_sank_a_winning_market_saved_at_the_typical_rate() -> None:
    # Other bets won 1.50, the breakout lost 2: the market paid nothing,
    # so its own rate is unknown and the typical one prices the saving.
    assert estimated_commission(-0.5, 0.0, -2.0, 0.05) == pytest.approx(-0.075)


@pytest.mark.parametrize(
    ("market_gross", "charged", "expected"),
    [
        # Other bets won 100.04 and the breakout lost 100.00: the 0.00
        # charged on a 4 cent win is rounding, not a rate of zero.
        (0.04, 0.0, -5.002),
        # A cent the other way, and the market lost: the same answer.
        (-0.01, 0.0, -4.9995),
        # One cent charged on a 10 cent win is not a 10% rate either.
        (0.10, 0.01, 0.01 - 0.05 * 100.10),
    ],
)
def test_a_market_that_barely_won_is_priced_at_the_typical_rate(
    market_gross, charged, expected
) -> None:
    estimate = estimated_commission(market_gross, charged, -100.0, 0.05)

    assert estimate == pytest.approx(expected)


def test_a_saving_cannot_be_priced_without_a_typical_rate() -> None:
    assert estimated_commission(-0.5, 0.0, -2.0, None) is None
    # No rate is needed when the other bets would have paid nothing anyway.
    assert estimated_commission(-12.0, 0.0, -2.0, None) == 0.0
    assert estimated_commission(200.0, 12.0, 250.0, None) == pytest.approx(12.0)


def test_a_loser_in_a_market_that_lost_anyway_changed_nothing() -> None:
    assert estimated_commission(-12.0, 0.0, -2.0, 0.05) == 0.0


# --- the report --------------------------------------------------------------


def test_breakout_reports_bets_winners_gross_commission_and_net_per_period() -> None:
    bets = [
        # Today: a winner alone, and a loser beside a winning lay.
        _subset_bet("1", "1.100", 198.0),
        _subset_bet("2", "1.200", -2.0),
        _bet("3", "1.200", 102.0),
        # Yesterday: a loser in a market that lost anyway.
        _subset_bet("4", "1.300", -2.0, YESTERDAY),
        _bet("5", "1.300", -10.0, YESTERDAY),
        # Last month: outside week and month, inside the year.
        _subset_bet("6", "1.400", -2.0, LAST_MONTH),
        # Never part of the breakout: the same side and order type in
        # another sport, and the other order type in this one.
        _bet("7", "1.500", 50.0, side="LAY", event_type=OTHER_SPORT),
        _bet("8", "1.600", 40.0, side="LAY", order_type="MARKET_ON_CLOSE"),
    ]
    store = _store(
        _commission("1.100", "11.88"),
        _commission("1.200", "6.00"),
        _commission("1.300", "0.00", YESTERDAY),
        _commission("1.400", "0.00", LAST_MONTH),
        _commission("1.500", "3.00"),
        _commission("1.600", "2.40"),
    )

    periods = _periods(_report(bets, store))

    today = periods["Today"]
    assert (today.bets, today.winners) == (2, 1)
    assert today.gross == pytest.approx(196.0)
    assert today.commission == pytest.approx(11.88 - 0.12)
    assert today.net == pytest.approx(196.0 - 11.76)
    assert periods["Yesterday"] == BreakoutFigures("Yesterday", 1, 0, -2.0, 0.0)
    assert periods["Week to date"].bets == 3
    assert periods["Month to date"].gross == pytest.approx(194.0)
    year = periods["Year to date"]
    assert (year.bets, year.winners) == (4, 1)
    assert year.gross == pytest.approx(192.0)
    assert list(periods) == [
        "Today",
        "Yesterday",
        "Week to date",
        "Month to date",
        "Year to date",
    ]


def test_breakout_section_follows_pending_and_leaves_the_account_figures_alone() -> (
    None
):
    bets = [_subset_bet("1", "1.100", 198.0), _bet("2", "1.200", 30.0)]
    store = _store(_commission("1.100", "11.88"), _commission("1.200", "1.80"))

    plain = build_daily_dm_report_from_dataframe(
        pd.DataFrame(bets), report_dt=REPORT_AT, market_commission=store
    )
    report = _report(bets, store)

    assert report.text.startswith(
        plain.text + "\n\n*Soccer lays* (commission estimated)"
    )
    assert (
        "• Today: net $186.12 (gross $198.00, commission $11.88) · 1 bet, 1 winner"
        in report.text
    )
    assert (
        "• Yesterday: net $0.00 (gross $0.00, commission $0.00) · 0 bets, 0 winners"
        in report.text
    )
    assert report.day_to_date == plain.day_to_date
    assert plain.breakouts == ()


def test_sunday_shows_last_week_in_place_of_week_to_date() -> None:
    sunday = datetime(2026, 6, 7, 9, 0, tzinfo=SYDNEY)
    bets = [_subset_bet("1", "1.100", -2.0, TODAY), _bet("2", "1.100", -5.0, TODAY)]

    report = build_daily_dm_report_from_dataframe(
        pd.DataFrame(bets),
        report_dt=sunday,
        market_commission=_store(_commission("1.100", "0.00")),
        breakouts=(SUBSET,),
    )

    periods = _periods(report)
    assert "Week to date" not in periods
    assert periods["Last week"].bets == 1 and periods["Today"].bets == 0


def test_a_market_with_unknown_commission_counts_as_zero_and_says_so() -> None:
    bets = [_subset_bet("1", "1.100", 198.0), _subset_bet("2", "1.200", 98.0)]
    store = _store(_commission("1.100", "11.88"))  # nothing for 1.200

    report = _report(bets, store)

    today = _periods(report)["Today"]
    assert today.commission == pytest.approx(11.88)
    assert today.unknown_markets == 1
    assert "2 bets, 2 winners · ⚠️ commission unknown for 1 market" in report.text


def test_a_saving_is_priced_at_the_typical_rate_of_the_markets_own_sport() -> None:
    lays = BreakoutSpec("Lays", side="LAY")
    bets = [
        # Soccer charges 5% and 7% on its ordinary winners (median 6%);
        # the other sport charges 50%.
        _bet("1", "1.100", 100.0),
        _bet("2", "1.200", 100.0),
        _bet("3", "1.300", 100.0, event_type=OTHER_SPORT),
        # In each sport the breakout's loss sank a market the other bet won.
        _subset_bet("4", "1.400", -2.0),
        _bet("5", "1.400", 1.5),
        _bet("6", "1.500", -2.0, side="LAY", event_type=OTHER_SPORT),
        _bet("7", "1.500", 1.5, event_type=OTHER_SPORT),
    ]
    store = _store(
        _commission("1.100", "5.00"),
        _commission("1.200", "7.00"),
        _commission("1.300", "50.00"),
        _commission("1.400", "0.00"),
        _commission("1.500", "0.00"),
    )

    today = _periods(_report(bets, store, lays))["Today"]

    assert today.commission == pytest.approx(-(0.06 + 0.50) * 1.5)
    assert today.unknown_markets == 0


def test_a_saving_with_no_typical_rate_to_price_it_is_unknown_not_zero() -> None:
    # The only market on file is the one the breakout sank.
    bets = [_subset_bet("1", "1.100", -2.0), _bet("2", "1.100", 1.5)]

    report = _report(bets, _store(_commission("1.100", "0.00")))

    today = _periods(report)["Today"]
    assert (today.commission, today.unknown_markets) == (0.0, 1)
    assert "1 bet, 0 winners · ⚠️ commission unknown for 1 market" in report.text


def test_bets_in_a_partially_settled_market_wait_for_its_close() -> None:
    bets = [_subset_bet("1", "1.100", -2.0), _subset_bet("2", "1.200", -2.0)]
    status = pd.DataFrame(
        [
            {
                "marketId": "1.200",
                "status": "OPEN",
                "activeRunners": "5",
                "source": "book",
                "checkedUtc": "2026-06-06T09:00:00Z",
                "firstPendingUtc": "2026-06-06T03:00:00Z",
                "closedObservedUtc": "",
            }
        ],
        columns=STATUS_COLUMNS,
        dtype=str,
    )

    report = build_daily_dm_report_from_dataframe(
        pd.DataFrame(bets),
        report_dt=REPORT_AT,
        market_commission=_store(_commission("1.100", "0.00")),
        market_status=status,
        breakouts=(SUBSET,),
    )

    assert _periods(report)["Year to date"].bets == 1
    assert report.pending.markets == 1


def test_a_bet_with_no_market_id_is_its_own_unknown_market() -> None:
    bets = [_subset_bet("1", "", 10.0), _subset_bet("2", "", 20.0)]

    today = _periods(_report(bets, _store()))["Today"]

    assert (today.bets, today.gross, today.unknown_markets) == (2, 30.0, 2)


def test_commission_lands_in_last_week_on_a_sunday() -> None:
    sunday = datetime(2026, 6, 7, 9, 0, tzinfo=SYDNEY)

    report = build_daily_dm_report_from_dataframe(
        pd.DataFrame([_subset_bet("1", "1.100", 198.0)]),
        report_dt=sunday,
        market_commission=_store(_commission("1.100", "11.88")),
        breakouts=(SUBSET,),
    )

    periods = _periods(report)
    assert periods["Last week"].commission == pytest.approx(11.88)
    assert periods["Today"].commission == 0.0


def test_a_commission_figure_from_after_the_report_time_is_unknown() -> None:
    # Rendered for 1 PM; the store row belongs to a 4 PM settlement.
    at = datetime(2026, 6, 6, 13, 0, tzinfo=SYDNEY)
    store = _store(_commission("1.100", "11.88", "2026-06-06T06:00:00Z"))

    report = build_daily_dm_report_from_dataframe(
        pd.DataFrame([_subset_bet("1", "1.100", 198.0)]),
        report_dt=at,
        market_commission=store,
        breakouts=(SUBSET,),
    )

    today = _periods(report)["Today"]
    assert (today.commission, today.unknown_markets) == (0.0, 1)


def test_the_heading_cannot_break_slack_formatting_or_mention_anyone() -> None:
    spec = BreakoutSpec("A*B <!channel> & co", side="LAY")

    report = _report([_subset_bet("1", "1.100", -2.0)], _store(), spec)

    assert "*AB &lt;!channel&gt; &amp; co* (commission estimated)" in report.text


def test_criteria_match_whatever_case_and_dtype_the_csv_gave_them() -> None:
    bets = [
        _bet("1", "1.100", -2.0, side=" lay ", order_type="limit", event_type="1"),
        _bet("2", "1.200", -2.0, side="LAY", event_type=1.0),
        _bet("3", "1.300", -2.0, side="LAY", event_type="x"),
    ]
    store = _store(
        _commission("1.100", "0.00"),
        _commission("1.200", "0.00"),
        _commission("1.300", "0.00"),
    )

    assert _periods(_report(bets, store))["Today"].bets == 2


def test_a_breakout_with_no_bets_still_shows_its_periods_at_zero() -> None:
    report = _report([_bet("1", "1.100", 10.0)], _store(_commission("1.100", "0.60")))

    periods = _periods(report)
    assert all(p.bets == 0 and p.net == 0.0 for p in periods.values())
    assert "• Today: net $0.00 (gross $0.00, commission $0.00) · 0 bets, 0 winners" in (
        report.text
    )


def test_a_missing_column_is_reported_in_the_section_not_raised() -> None:
    frame = pd.DataFrame([_subset_bet("1", "1.100", -2.0)]).drop(columns=["orderType"])

    report = build_daily_dm_report_from_dataframe(
        frame, report_dt=REPORT_AT, breakouts=(SUBSET,)
    )

    assert report.breakouts[0].problem == "the results have no 'orderType' column"
    assert "*Soccer lays* (commission estimated)" in report.text
    assert "• ⚠️ Not reported: the results have no 'orderType' column" in report.text
    assert "• *Total: net -$2.00*" in report.text, "the account figures still go out"


def test_a_breakout_that_fails_to_compute_does_not_cost_the_report(monkeypatch) -> None:
    def boom(*_args, **_kwargs):
        raise RuntimeError("secret detail that must not reach Slack")

    monkeypatch.setattr(
        "betfair_results_downloader.reporting.daily_dm_report.build_breakout", boom
    )

    report = _report([_subset_bet("1", "1.100", -2.0)], _store())

    assert "• ⚠️ Not reported: RuntimeError while computing it" in report.text
    assert "secret detail" not in report.text
    assert "• *Total: net -$2.00*" in report.text


# --- configuration -----------------------------------------------------------


def test_parse_reads_a_definition_and_normalises_its_text() -> None:
    creds = {
        "report": {
            "breakouts": [
                {
                    "name": " Soccer lays ",
                    "event_type_id": 1,
                    "side": "lay",
                    "order_type": "limit",
                }
            ]
        }
    }

    assert parse_breakouts(creds) == ((SUBSET,), ())


@pytest.mark.parametrize(
    "creds", [None, {}, {"report": {}}, {"report": {"breakouts": []}}]
)
def test_parse_without_breakouts_is_quietly_empty(creds) -> None:
    assert parse_breakouts(creds) == ((), ())


@pytest.mark.parametrize(
    ("entry", "problem"),
    [
        ("Soccer lays", "breakout 1: not an object"),
        ({"side": "BACK"}, "breakout 1: it has no name"),
        (
            {"name": "A"},
            "breakout 1: it selects every bet; give it at least one criterion",
        ),
        ({"name": "A", "sides": "BACK"}, "breakout 1: unknown setting 'sides'"),
        (
            {"name": "A", "event_type_id": "7"},
            "breakout 1: event_type_id is not a whole number",
        ),
        (
            {"name": "A", "event_type_id": True},
            "breakout 1: event_type_id is not a whole number",
        ),
        ({"name": "A", "side": 1}, "breakout 1: side is not text"),
    ],
)
def test_parse_names_what_is_wrong_with_an_entry(entry, problem) -> None:
    assert parse_breakouts({"report": {"breakouts": [entry]}}) == ((), (problem,))


def test_parse_keeps_the_good_entries_beside_a_bad_one() -> None:
    good = {"name": "A", "side": "BACK"}
    creds = {
        "report": {"breakouts": [good, {"name": "A", "side": "LAY"}, {"name": "B"}]}
    }

    specs, problems = parse_breakouts(creds)

    assert specs == (BreakoutSpec("A", side="BACK"),)
    assert problems == (
        "breakout 2: the name is used twice",
        "breakout 3: it selects every bet; give it at least one criterion",
    )


@pytest.mark.parametrize(
    ("creds", "problem"),
    [
        ({"report": []}, "the 'report' setting is not an object"),
        ({"report": {"breakouts": {}}}, "'report.breakouts' is not a list"),
    ],
)
def test_parse_reports_a_setting_of_the_wrong_shape(creds, problem) -> None:
    assert parse_breakouts(creds) == ((), (problem,))


# --- the command -------------------------------------------------------------


def _results_dir(tmp_path) -> str:
    results = tmp_path / "results"
    results.mkdir()
    pd.DataFrame([_subset_bet("1", "1.100", 198.0), _bet("2", "1.200", 30.0)]).to_csv(
        results / "cleared_orders_cleaned.csv", index=False
    )
    return str(results)


def _run_dm_report(monkeypatch, capsys, creds: dict) -> str:
    monkeypatch.setattr(
        "betfair_results_downloader.__main__._load_creds_and_schedule",
        lambda *_a, **_k: (creds, None),
    )
    assert main(["dm-report", "--at", "2026-06-06T21:00:00+10:00"]) == 0
    return capsys.readouterr().out


def test_dm_report_prints_the_breakouts_configured_in_credentials(
    tmp_path, capsys, monkeypatch
) -> None:
    creds = {
        "paths": {"results_csv_dir": _results_dir(tmp_path)},
        "report": {
            "breakouts": [
                {
                    "name": "Soccer lays",
                    "event_type_id": 1,
                    "side": "LAY",
                    "order_type": "LIMIT",
                }
            ]
        },
    }

    out = _run_dm_report(monkeypatch, capsys, creds)

    assert "*Soccer lays* (commission estimated)" in out
    # No commission store beside the CSV: unknown, and said so.
    assert (
        "• Today: net $198.00 (gross $198.00, commission $0.00) · 1 bet, 1 winner"
        " · ⚠️ commission unknown for 1 market" in out
    )


def test_dm_report_without_the_setting_is_unchanged(
    tmp_path, capsys, monkeypatch
) -> None:
    creds = {"paths": {"results_csv_dir": _results_dir(tmp_path)}}

    out = _run_dm_report(monkeypatch, capsys, creds)

    assert "commission estimated" not in out and "Breakout" not in out
    assert out.rstrip().endswith("• None")


def test_dm_report_warns_about_a_definition_it_had_to_ignore(
    tmp_path, capsys, monkeypatch
) -> None:
    creds = {
        "paths": {"results_csv_dir": _results_dir(tmp_path)},
        "report": {"breakouts": [{"name": "Soccer lays", "sides": "BACK"}]},
    }

    out = _run_dm_report(monkeypatch, capsys, creds)

    assert "⚠️ Breakout setting ignored: breakout 1: unknown setting 'sides'" in out
    assert "• *Total: net $228.00*" in out

"""
The scheduled pipeline records per-market commission every run.

The commission step shares the run's logged-in client, runs after the status
step (so it re-queries on fresh pending data) and before Azure, must never
fail the run, and a failure must be announced rather than swallowed.
"""

from __future__ import annotations

import inspect
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pandas as pd

from betfair_results_downloader.commission import (
    COMMISSION_FILENAME,
    load_market_commission,
)
from betfair_results_downloader.config import ScheduleConfig
from betfair_results_downloader.downloader_core import (
    CsvWriteResult,
    DownloadResult,
    EnrichResult,
)
from betfair_results_downloader.market_status import (
    STATUS_FILENAME,
    save_market_status,
)
from betfair_results_downloader.scheduler import runner
from betfair_results_downloader.scheduler.runner import _run_pipeline

FROM_DT = datetime(2026, 9, 12, 17, 30, tzinfo=timezone.utc)
TO_DT = datetime(2026, 9, 12, 19, 30, tzinfo=timezone.utc)


def _creds(tmp_path: Path) -> dict:
    return {
        "betfair": {
            "username": "u",
            "password": "p",
            "app_key": "k",
            "certs_dir": str(tmp_path),
        },
        "user": {"enable_azure_sql": False, "dry_run": True},
        "paths": {"results_csv_dir": str(tmp_path)},
        "azure_sql": {},
        "schedule": {"enabled": True},
    }


def _window() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "betId": ["1", "2"],
            "eventTypeId": ["7", "7"],
            "marketId": ["1.234", "1.235"],
            "profit": ["5.0", "-2.0"],
            "placedDate": ["2026-09-12T17:45:00Z", "2026-09-12T17:45:00Z"],
            "settledDate": ["2026-09-12T18:00:00Z", "2026-09-12T18:05:00Z"],
        }
    )


def _pipeline_patches(tmp_path: Path, df: pd.DataFrame, client: MagicMock):
    dl = DownloadResult(attempted=True, rows_downloaded=len(df), message="ok", df_co=df)
    enr = (
        df,
        EnrichResult(
            attempted=True, markets_requested=0, markets_returned=0, message="ok"
        ),
    )
    csvr = CsvWriteResult(
        canonical_path=tmp_path / "cleared_orders_cleaned.csv",
        snapshot_path=tmp_path / "snap.csv.gz",
        rows_in_canonical=len(df),
        message="ok",
        df_canonical=df,
    )
    return (
        patch(
            "betfair_results_downloader.scheduler.runner.build_api_client",
            return_value=client,
        ),
        patch(
            "betfair_results_downloader.downloader_core.fetch_cleared_orders_df_range",
            return_value=dl,
        ),
        patch(
            "betfair_results_downloader.downloader_core.enrich_with_market_catalogue",
            return_value=enr,
        ),
        patch(
            "betfair_results_downloader.downloader_core.write_csv_outputs",
            return_value=csvr,
        ),
    )


def _grouped(market_id: str, profit: float, commission: float) -> dict:
    return {
        "eventTypeId": "7",
        "marketId": market_id,
        "settledDate": "2026-09-12T18:05:00.000Z",
        "betCount": 1,
        "profit": profit,
        "commission": commission,
    }


def _client(grouped: list[dict]) -> MagicMock:
    client = MagicMock()
    client.betting.list_market_book.return_value = [
        SimpleNamespace(market_id="1.234", status="CLOSED", number_of_active_runners=0),
        SimpleNamespace(market_id="1.235", status="CLOSED", number_of_active_runners=0),
    ]
    client.betting.list_cleared_orders.return_value = {
        "clearedOrders": grouped,
        "moreAvailable": False,
    }
    return client


def test_commission_file_is_written_from_the_same_logged_in_client(
    tmp_path: Path,
) -> None:
    client = _client([_grouped("1.234", 5.0, 0.35), _grouped("1.235", -2.0, 0.0)])
    p1, p2, p3, p4 = _pipeline_patches(tmp_path, _window(), client)

    with p1, p2, p3, p4:
        result = _run_pipeline(_creds(tmp_path), ScheduleConfig(), FROM_DT, TO_DT)

    assert result.ok is True and result.status == "success"
    assert "⚠️" not in result.message
    calls = [c.kwargs for c in client.betting.list_cleared_orders.call_args_list]
    window = [c for c in calls if "market_ids" not in c]
    assert len(window) == 1
    assert window[0]["group_by"] == "MARKET"
    assert window[0]["settled_date_range"] == {
        "from": "2026-09-12T17:30:00Z",
        "to": "2026-09-12T19:30:00Z",
    }
    # Both markets came back in the window, so the recent seed had nothing
    # left to ask for by id.
    assert all("market_ids" in c for c in calls[1:]) and len(calls) == 1
    stored = load_market_commission(tmp_path / ".cache" / COMMISSION_FILENAME)
    by_id = stored.set_index("marketId")
    assert by_id.loc["1.234", "commission"] == "0.35"
    assert by_id.loc["1.235", "commission"] == "0.00"
    assert by_id.loc["1.234", "grossProfit"] == "5.00"


def test_commission_failure_is_non_fatal_but_announced(tmp_path: Path) -> None:
    client = _client([])
    client.betting.list_cleared_orders.side_effect = RuntimeError("grouped unavailable")
    p1, p2, p3, p4 = _pipeline_patches(tmp_path, _window(), client)

    with p1, p2, p3, p4:
        result = _run_pipeline(_creds(tmp_path), ScheduleConfig(), FROM_DT, TO_DT)

    assert result.ok is True and result.status == "success"
    assert "⚠️ Commission read failed" in result.message
    assert "grouped unavailable" in result.message
    assert not (tmp_path / ".cache" / COMMISSION_FILENAME).exists()
    # The status step is independent and still wrote its file.
    assert (tmp_path / ".cache" / STATUS_FILENAME).exists()


def test_commission_step_runs_after_status_and_before_azure() -> None:
    """
    After the status step so the pending re-queries see this run's
    observations; before Azure so a publish failure's early return cannot
    skip it.
    """
    source = inspect.getsource(runner._run_pipeline_inner)
    status_at = source.index(
        "_refresh_market_status(", source.index("write_csv_outputs(")
    )
    commission_at = source.index("_refresh_market_commission(", status_at)
    azure_at = source.index("prepare_azure_dataset(")
    assert status_at < commission_at < azure_at


def test_empty_download_still_requeries_pending_markets(tmp_path: Path) -> None:
    """
    The run that finally reads an outright's commission is the one after the
    status step saw it CLOSED, which is usually a run with nothing to
    download. Returning early there would leave the 0.0 placeholder forever.
    """
    save_market_status(
        pd.DataFrame(
            [
                {
                    "marketId": "1.247612484",
                    "status": "OPEN",
                    "activeRunners": "3",
                    "source": "book",
                    "checkedUtc": "2026-09-11T18:00:00Z",
                    "firstPendingUtc": "2026-09-07T20:00:00Z",
                    "closedObservedUtc": "",
                }
            ]
        ),
        tmp_path / ".cache" / STATUS_FILENAME,
    )
    client = MagicMock()
    client.betting.list_market_book.return_value = []  # absent: closed now
    client.betting.list_cleared_orders.return_value = {
        "clearedOrders": [_grouped("1.247612484", 11.19, 0.6)],
        "moreAvailable": False,
    }
    empty = DownloadResult(
        attempted=True, rows_downloaded=0, message="none", df_co=pd.DataFrame()
    )

    with (
        patch(
            "betfair_results_downloader.scheduler.runner.build_api_client",
            return_value=client,
        ),
        patch(
            "betfair_results_downloader.downloader_core.fetch_cleared_orders_df_range",
            return_value=empty,
        ),
    ):
        result = _run_pipeline(_creds(tmp_path), ScheduleConfig(), FROM_DT, TO_DT)

    assert result.ok is True and result.status == "success"
    assert "⚠️" not in result.message
    calls = client.betting.list_cleared_orders.call_args_list
    explicit = [c.kwargs for c in calls if "market_ids" in c.kwargs]
    assert explicit and explicit[0]["market_ids"] == ["1.247612484"]
    stored = load_market_commission(tmp_path / ".cache" / COMMISSION_FILENAME)
    assert stored.set_index("marketId").loc["1.247612484", "commission"] == "0.60"


def test_missed_window_is_reread_from_the_canonical_on_the_next_run(
    tmp_path: Path,
) -> None:
    """
    The 06:00 read fails; the 09:00 window never touches those markets
    again. The recent-market seed reads the canonical's unread markets by
    id so the report does not show them commission-unknown for good.
    """
    canonical = _window()  # 1.234 and 1.235 settled 2026-09-12, store empty
    client = MagicMock()
    client.betting.list_market_book.return_value = []
    grouped = {
        "1.234": _grouped("1.234", 5.0, 0.35),
        "1.235": _grouped("1.235", -2.0, 0.0),
        "1.999": _grouped("1.999", 1.0, 0.07),
    }

    def serve(**kwargs):
        # The 20:00-21:00 window read returns nothing for the missed
        # markets; only an explicit-id read reaches them.
        ids = kwargs.get("market_ids") or []
        return {
            "clearedOrders": [grouped[m] for m in ids if m in grouped],
            "moreAvailable": False,
        }

    client.betting.list_cleared_orders.side_effect = serve
    later_window = pd.DataFrame(
        {
            "betId": ["9"],
            "eventTypeId": ["7"],
            "marketId": ["1.999"],
            "profit": ["1.0"],
            "placedDate": ["2026-09-12T20:00:00Z"],
            "settledDate": ["2026-09-12T20:30:00Z"],
        }
    )
    dl = DownloadResult(
        attempted=True, rows_downloaded=1, message="ok", df_co=later_window
    )
    csvr = CsvWriteResult(
        canonical_path=tmp_path / "cleared_orders_cleaned.csv",
        snapshot_path=tmp_path / "snap.csv.gz",
        rows_in_canonical=3,
        message="ok",
        df_canonical=pd.concat([canonical, later_window], ignore_index=True),
    )
    with (
        patch(
            "betfair_results_downloader.scheduler.runner.build_api_client",
            return_value=client,
        ),
        patch(
            "betfair_results_downloader.downloader_core.fetch_cleared_orders_df_range",
            return_value=dl,
        ),
        patch(
            "betfair_results_downloader.downloader_core.enrich_with_market_catalogue",
            return_value=(later_window, EnrichResult(True, 0, 0, "ok")),
        ),
        patch(
            "betfair_results_downloader.downloader_core.write_csv_outputs",
            return_value=csvr,
        ),
        patch("betfair_results_downloader.commission._now_utc", return_value=TO_DT),
    ):
        result = _run_pipeline(
            _creds(tmp_path),
            ScheduleConfig(),
            datetime(2026, 9, 12, 20, 0, tzinfo=timezone.utc),
            datetime(2026, 9, 12, 21, 0, tzinfo=timezone.utc),
        )

    assert result.ok is True and "⚠️" not in result.message
    explicit = [
        c.kwargs["market_ids"]
        for c in client.betting.list_cleared_orders.call_args_list
        if "market_ids" in c.kwargs
    ]
    assert explicit and sorted(explicit[0]) == ["1.234", "1.235", "1.999"]
    stored = load_market_commission(tmp_path / ".cache" / COMMISSION_FILENAME)
    assert stored.set_index("marketId").loc["1.234", "commission"] == "0.35"


def test_empty_download_commission_failure_is_announced(tmp_path: Path) -> None:
    client = MagicMock()
    client.betting.list_market_book.return_value = []
    client.betting.list_cleared_orders.side_effect = RuntimeError("grouped down")
    empty = DownloadResult(
        attempted=True, rows_downloaded=0, message="none", df_co=pd.DataFrame()
    )

    with (
        patch(
            "betfair_results_downloader.scheduler.runner.build_api_client",
            return_value=client,
        ),
        patch(
            "betfair_results_downloader.downloader_core.fetch_cleared_orders_df_range",
            return_value=empty,
        ),
    ):
        result = _run_pipeline(_creds(tmp_path), ScheduleConfig(), FROM_DT, TO_DT)

    assert result.ok is True and result.status == "success"
    assert "⚠️ Commission read failed" in result.message
    assert "grouped down" in result.message

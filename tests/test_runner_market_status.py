"""
The scheduled pipeline records market settlement status every run.

The status must be captured in the same run as the download (CLOSED markets
leave listMarketBook after a variable period), must never fail the run, and
a failure must be announced rather than swallowed.
"""

from __future__ import annotations

import inspect
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pandas as pd

from betfair_results_downloader.config import ScheduleConfig
from betfair_results_downloader.downloader_core import (
    CsvWriteResult,
    DownloadResult,
    EnrichResult,
)
from betfair_results_downloader.market_status import (
    STATUS_FILENAME,
    load_market_status,
)
from betfair_results_downloader.scheduler import runner
from betfair_results_downloader.scheduler.runner import _run_pipeline

FROM_DT = datetime(2026, 6, 6, 17, 30, tzinfo=timezone.utc)
TO_DT = datetime(2026, 6, 6, 19, 30, tzinfo=timezone.utc)


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
            "eventTypeId": ["7", "2"],
            "marketId": ["1.234", "1.247612197"],
            "profit": ["5.0", "0.23"],
            "placedDate": ["2026-06-06T17:45:00Z", "2026-06-06T17:45:00Z"],
            "settledDate": ["2026-06-06T18:00:00Z", "2026-06-06T18:05:00Z"],
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


def _book(market_id: str, status: str, active: int) -> SimpleNamespace:
    return SimpleNamespace(
        market_id=market_id, status=status, number_of_active_runners=active
    )


def test_status_file_is_written_from_the_same_logged_in_client(tmp_path: Path) -> None:
    client = MagicMock()
    client.betting.list_market_book.return_value = [
        _book("1.234", "CLOSED", 0),
        _book("1.247612197", "OPEN", 22),
    ]
    df = _window()
    p1, p2, p3, p4 = _pipeline_patches(tmp_path, df, client)

    with p1, p2, p3, p4:
        result = _run_pipeline(_creds(tmp_path), ScheduleConfig(), FROM_DT, TO_DT)

    assert result.ok is True and result.status == "success"
    assert "⚠️" not in result.message
    client.betting.list_market_book.assert_called_once()
    requested = client.betting.list_market_book.call_args.kwargs["market_ids"]
    assert sorted(requested) == ["1.234", "1.247612197"]

    status = load_market_status(tmp_path / ".cache" / STATUS_FILENAME)
    by_id = status.set_index("marketId")
    assert by_id.loc["1.234", "status"] == "CLOSED"
    assert by_id.loc["1.234", "firstPendingUtc"] == ""
    assert by_id.loc["1.247612197", "status"] == "OPEN"
    assert by_id.loc["1.247612197", "firstPendingUtc"] != ""
    assert by_id.loc["1.247612197", "closedObservedUtc"] == ""


def test_status_failure_is_non_fatal_but_announced(tmp_path: Path) -> None:
    client = MagicMock()
    df = _window()
    p1, p2, p3, p4 = _pipeline_patches(tmp_path, df, client)

    with (
        p1,
        p2,
        p3,
        p4,
        patch(
            "betfair_results_downloader.market_status.update_market_status",
            side_effect=RuntimeError("book unavailable"),
        ),
    ):
        result = _run_pipeline(_creds(tmp_path), ScheduleConfig(), FROM_DT, TO_DT)

    assert result.ok is True
    assert result.status == "success"
    # The ⚠️ is what _cmd_run keys on to post a "succeeded with a warning"
    # to Slack; a silent degradation here would be exactly the failure mode
    # this project keeps paying for.
    assert "⚠️ Market settlement status check failed" in result.message
    assert "book unavailable" in result.message
    assert not (tmp_path / ".cache" / STATUS_FILENAME).exists()


def test_status_step_runs_after_csv_write_and_before_azure() -> None:
    """
    After the CSV write so the canonical frame is available for the
    self-healing recheck; before Azure so a publish failure's early return
    cannot skip it.
    """
    source = inspect.getsource(runner._run_pipeline_inner)
    csv_at = source.index("write_csv_outputs(")
    status_at = source.index("_refresh_market_status(", csv_at)
    azure_at = source.index("prepare_azure_dataset(")
    assert csv_at < status_at < azure_at
    assert "csvr.df_canonical" in source[status_at:azure_at]


def _pending_status_file(tmp_path: Path) -> Path:
    from betfair_results_downloader.market_status import save_market_status

    path = tmp_path / ".cache" / STATUS_FILENAME
    save_market_status(
        pd.DataFrame(
            [
                {
                    "marketId": "1.247612197",
                    "status": "OPEN",
                    "activeRunners": "22",
                    "source": "book",
                    "checkedUtc": "2026-06-05T18:00:00Z",
                    "firstPendingUtc": "2026-06-05T18:00:00Z",
                    "closedObservedUtc": "",
                }
            ]
        ),
        path,
    )
    return path


def test_empty_download_still_rechecks_pending_markets(tmp_path: Path) -> None:
    """
    The run that finally sees an outright CLOSED is usually a quiet one: the
    user's bets all settled days earlier, so there is nothing to download.
    Returning early there would leave the market pending forever.
    """
    path = _pending_status_file(tmp_path)
    client = MagicMock()
    client.betting.list_market_book.return_value = []  # absent: closed
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
    assert result.last_confirmed_settled_at_utc is None, "empty run confirms nothing"
    requested = client.betting.list_market_book.call_args.kwargs["market_ids"]
    assert requested == ["1.247612197"]
    row = load_market_status(path).set_index("marketId").loc["1.247612197"]
    assert row["status"] == "CLOSED"
    assert row["source"] == "absent"
    assert row["closedObservedUtc"] != ""


def test_empty_download_status_failure_is_announced(tmp_path: Path) -> None:
    _pending_status_file(tmp_path)
    client = MagicMock()
    client.betting.list_market_book.side_effect = RuntimeError("book unavailable")
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
    assert "⚠️ Market settlement status check failed" in result.message
    assert "book unavailable" in result.message

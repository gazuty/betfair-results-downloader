"""
Pipeline wiring for the racecard and results-feed steps: where they run,
that they run on a quiet window too, and that a failure in either is
announced without failing the download.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd

from betfair_results_downloader.config import ScheduleConfig
from betfair_results_downloader.downloader_core import (
    CsvWriteResult,
    DownloadResult,
    EnrichResult,
)
from betfair_results_downloader.market_results import FEED_FILENAME, META_FILENAME
from betfair_results_downloader.racecard import RACECARD_FILENAME
from betfair_results_downloader.scheduler import runner
from betfair_results_downloader.scheduler.runner import _run_pipeline

FROM_DT = datetime(2026, 11, 14, 6, 0, tzinfo=timezone.utc)
TO_DT = datetime(2026, 11, 14, 8, 0, tzinfo=timezone.utc)


def _creds(tmp_path: Path) -> dict:
    return {
        "betfair": {"username": "u", "password": "p", "app_key": "k"},
        "user": {"enable_azure_sql": False, "dry_run": True},
        "paths": {"results_csv_dir": str(tmp_path)},
        "azure_sql": {},
    }


def _window() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "betId": ["1"],
            "eventTypeId": ["1"],
            "marketId": ["1.100"],
            "profit": ["5.0"],
            "placedDate": ["2026-11-14T06:30:00Z"],
            "settledDate": ["2026-11-14T07:00:00.000Z"],
            "evt_eventName": ["A v B"],
            "mkt_marketName": ["Correct Score"],
        }
    )


def _client() -> MagicMock:
    client = MagicMock()
    client.betting.list_market_book.return_value = []
    client.betting.list_cleared_orders.return_value = {
        "clearedOrders": [],
        "moreAvailable": False,
    }
    client.betting.list_market_catalogue.return_value = []
    return client


def _patches(tmp_path: Path, df: pd.DataFrame, client: MagicMock):
    def write_canonical(*, df_co, results_csv_dir, **_kw):
        # The feed reads the canonical from disk, as it does in production.
        df_co.to_csv(Path(results_csv_dir) / "cleared_orders_cleaned.csv", index=False)
        return CsvWriteResult(
            canonical_path=Path(results_csv_dir) / "cleared_orders_cleaned.csv",
            snapshot_path=Path(results_csv_dir) / "snap.csv.gz",
            rows_in_canonical=len(df_co),
            message="ok",
            df_canonical=df_co,
        )

    return (
        patch.object(runner, "build_api_client", return_value=client),
        patch(
            "betfair_results_downloader.downloader_core.fetch_cleared_orders_df_range",
            return_value=DownloadResult(
                attempted=True, rows_downloaded=len(df), message="ok", df_co=df
            ),
        ),
        patch(
            "betfair_results_downloader.downloader_core.enrich_with_market_catalogue",
            return_value=(
                df,
                EnrichResult(
                    attempted=True,
                    markets_requested=0,
                    markets_returned=0,
                    message="ok",
                ),
            ),
        ),
        patch(
            "betfair_results_downloader.downloader_core.write_csv_outputs",
            side_effect=write_canonical,
        ),
    )


def test_a_run_captures_the_racecard_and_writes_the_feed(tmp_path: Path) -> None:
    client = _client()
    p1, p2, p3, p4 = _patches(tmp_path, _window(), client)
    with p1, p2, p3, p4:
        result = _run_pipeline(_creds(tmp_path), ScheduleConfig(), FROM_DT, TO_DT)

    assert result.ok and result.status == "success"
    client.betting.list_market_catalogue.assert_called()
    assert (tmp_path / ".cache" / RACECARD_FILENAME).exists()
    assert (tmp_path / FEED_FILENAME).exists()
    assert (tmp_path / META_FILENAME).exists()


def test_the_feed_is_built_after_the_status_and_commission_steps(
    tmp_path: Path,
) -> None:
    order: list[str] = []
    p1, p2, p3, p4 = _patches(tmp_path, _window(), _client())
    with (
        p1,
        p2,
        p3,
        p4,
        patch.object(
            runner,
            "_refresh_market_status",
            side_effect=lambda *a, **k: order.append("status"),
        ),
        patch.object(
            runner,
            "_refresh_market_commission",
            side_effect=lambda *a, **k: order.append("commission"),
        ),
        patch.object(
            runner,
            "_refresh_racecard",
            side_effect=lambda *a, **k: order.append("racecard"),
        ),
        patch.object(
            runner,
            "_publish_market_results",
            side_effect=lambda *a, **k: order.append("feed") or [],
        ),
    ):
        _run_pipeline(_creds(tmp_path), ScheduleConfig(), FROM_DT, TO_DT)

    # The feed must see this run's closes and commission reads.
    assert order == ["status", "commission", "racecard", "feed"]


def test_a_quiet_run_still_rebuilds_the_feed(tmp_path: Path) -> None:
    pd.DataFrame(_window()).to_csv(tmp_path / "cleared_orders_cleaned.csv", index=False)
    empty = DownloadResult(
        attempted=True, rows_downloaded=0, message="none", df_co=pd.DataFrame()
    )
    with (
        patch.object(runner, "build_api_client", return_value=_client()),
        patch(
            "betfair_results_downloader.downloader_core.fetch_cleared_orders_df_range",
            return_value=empty,
        ),
    ):
        result = _run_pipeline(_creds(tmp_path), ScheduleConfig(), FROM_DT, TO_DT)

    assert result.ok and result.status == "success"
    assert (tmp_path / FEED_FILENAME).exists()


def test_failures_in_either_step_are_announced_and_the_run_succeeds(
    tmp_path: Path,
) -> None:
    client = _client()
    client.betting.list_market_catalogue.side_effect = RuntimeError("catalogue down")
    p1, p2, p3, p4 = _patches(tmp_path, _window(), client)
    with (
        p1,
        p2,
        p3,
        p4,
        patch(
            "betfair_results_downloader.market_results.build_market_results_from_disk",
            side_effect=ValueError("bad canonical"),
        ),
    ):
        result = _run_pipeline(_creds(tmp_path), ScheduleConfig(), FROM_DT, TO_DT)

    assert result.ok and result.status == "success"
    assert "⚠️ Racecard capture failed" in result.message
    assert "catalogue down" in result.message
    assert "⚠️ Results feed not rebuilt" in result.message
    assert not (tmp_path / FEED_FILENAME).exists()


def test_a_stuck_commission_warning_from_the_feed_reaches_the_message(
    tmp_path: Path,
) -> None:
    p1, p2, p3, p4 = _patches(tmp_path, _window(), _client())
    with (
        p1,
        p2,
        p3,
        p4,
        patch.object(
            runner, "_publish_market_results", return_value=["⚠️ Results feed: stuck"]
        ),
    ):
        result = _run_pipeline(_creds(tmp_path), ScheduleConfig(), FROM_DT, TO_DT)

    assert "⚠️ Results feed: stuck" in result.message


def test_a_failed_status_check_keeps_the_previous_feed(tmp_path: Path) -> None:
    (tmp_path / FEED_FILENAME).write_text("previous")
    client = _client()
    client.betting.list_market_book.side_effect = RuntimeError("book down")
    p1, p2, p3, p4 = _patches(tmp_path, _window(), client)
    with p1, p2, p3, p4:
        result = _run_pipeline(_creds(tmp_path), ScheduleConfig(), FROM_DT, TO_DT)

    assert result.ok and result.status == "success"
    assert "settlement status check failed" in result.message
    assert (tmp_path / FEED_FILENAME).read_text() == "previous"

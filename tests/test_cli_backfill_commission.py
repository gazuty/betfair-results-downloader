"""CLI surface of ``backfill-commission``: argument checks and the happy path."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

from betfair_results_downloader.__main__ import main
from betfair_results_downloader.commission import COMMISSION_FILENAME, CommissionResult


def _creds(tmp_path: Path) -> dict:
    return {
        "betfair": {"username": "u", "password": "p", "app_key": "k"},
        "paths": {"results_csv_dir": str(tmp_path)},
        "user": {},
        "schedule": {"chunk_days": 7},
    }


def test_backfill_commission_requires_both_dates(capsys) -> None:
    assert main(["backfill-commission", "--from", "2026-01-01"]) == 2
    assert "--from and --to are required" in capsys.readouterr().out


def test_backfill_commission_rejects_bad_dates(capsys) -> None:
    assert (
        main(["backfill-commission", "--from", "2026-13-01", "--to", "2026-01-02"]) == 2
    )
    assert "invalid date format" in capsys.readouterr().out
    assert (
        main(["backfill-commission", "--from", "2026-02-01", "--to", "2026-01-02"]) == 2
    )
    assert "is after" in capsys.readouterr().out


def test_backfill_commission_wires_the_range_and_client_into_the_update(
    tmp_path, capsys, monkeypatch
) -> None:
    from betfair_results_downloader.config import ScheduleConfig

    client = MagicMock()
    result = CommissionResult(
        attempted=True,
        fetched=3,
        requeried=0,
        message="Commission: read 3 market(s) in window and 0 re-queried; "
        "3 market(s) on file; 1.50 commission in this read.",
        path=tmp_path / ".cache" / COMMISSION_FILENAME,
    )
    monkeypatch.setattr(
        "betfair_results_downloader.__main__._load_creds_and_schedule",
        lambda validate=False: (_creds(tmp_path), ScheduleConfig(chunk_days=7)),
    )
    with (
        patch(
            "betfair_results_downloader.scheduler.auth.build_api_client",
            return_value=client,
        ),
        patch(
            "betfair_results_downloader.commission.update_market_commission",
            return_value=result,
        ) as update,
    ):
        code = main(
            ["backfill-commission", "--from", "2026-01-01", "--to", "2026-01-31"]
        )

    assert code == 0
    assert "OK: Commission: read 3 market(s)" in capsys.readouterr().out
    kwargs = update.call_args.kwargs
    assert kwargs["client"] is client
    assert kwargs["cache_dir"] == tmp_path / ".cache"
    assert kwargs["from_dt"] == datetime(2026, 1, 1, tzinfo=timezone.utc)
    # Exclusive upper bound at midnight after --to, so the last day is whole.
    assert kwargs["to_dt"] == datetime(2026, 2, 1, tzinfo=timezone.utc)
    assert kwargs["chunk_days"] == 7
    client.logout.assert_called_once()


def test_backfill_commission_warns_when_the_range_predates_availability(
    tmp_path, capsys, monkeypatch
) -> None:
    from betfair_results_downloader.config import ScheduleConfig

    monkeypatch.setattr(
        "betfair_results_downloader.__main__._load_creds_and_schedule",
        lambda validate=False: (_creds(tmp_path), ScheduleConfig()),
    )
    result = CommissionResult(attempted=True, fetched=0, requeried=0, message="nothing")
    with (
        patch(
            "betfair_results_downloader.scheduler.auth.build_api_client",
            return_value=MagicMock(),
        ),
        patch(
            "betfair_results_downloader.commission.update_market_commission",
            return_value=result,
        ),
    ):
        code = main(
            ["backfill-commission", "--from", "2019-01-01", "--to", "2019-01-31"]
        )

    out = capsys.readouterr().out
    assert code == 0
    assert "WARNING: --from 2019-01-01 is more than 365 days ago" in out


def test_backfill_commission_reports_a_failed_read(
    tmp_path, capsys, monkeypatch
) -> None:
    from betfair_results_downloader.config import ScheduleConfig

    monkeypatch.setattr(
        "betfair_results_downloader.__main__._load_creds_and_schedule",
        lambda validate=False: (_creds(tmp_path), ScheduleConfig()),
    )
    with (
        patch(
            "betfair_results_downloader.scheduler.auth.build_api_client",
            return_value=MagicMock(),
        ),
        patch(
            "betfair_results_downloader.commission.update_market_commission",
            side_effect=RuntimeError("grouped unavailable"),
        ),
    ):
        code = main(
            ["backfill-commission", "--from", "2026-01-01", "--to", "2026-01-02"]
        )

    assert code == 1
    assert "FAIL: commission backfill raised RuntimeError: grouped unavailable" in (
        capsys.readouterr().out
    )

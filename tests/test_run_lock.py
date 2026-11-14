"""
The pipeline lock: one writing command at a time per results directory.

Unit tests for the lock itself, then the wiring: every command that
rewrites the canonical, a store or the feed must take it, and a command
refused by it must have changed nothing.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from betfair_results_downloader import run_lock
from betfair_results_downloader.__main__ import main
from betfair_results_downloader.config import ScheduleConfig
from betfair_results_downloader.market_results import FEED_FILENAME
from betfair_results_downloader.run_lock import (
    LOCK_FILENAME,
    PipelineBusy,
    pipeline_lock,
)
from betfair_results_downloader.scheduler import runner

FROM_DT = datetime(2026, 11, 14, 6, 0, tzinfo=timezone.utc)
TO_DT = datetime(2026, 11, 14, 8, 0, tzinfo=timezone.utc)


def _creds(tmp_path: Path) -> dict:
    return {
        "betfair": {"username": "u", "password": "p", "app_key": "k"},
        "user": {"enable_azure_sql": False, "dry_run": True},
        "paths": {"results_csv_dir": str(tmp_path)},
        "azure_sql": {},
        "schedule": {},
    }


# -----------------------------
# The lock
# -----------------------------


def test_the_lock_names_its_holder_and_clears_the_note_on_release(tmp_path) -> None:
    with pipeline_lock(tmp_path, command="backfill"):
        note = (tmp_path / LOCK_FILENAME).read_text()
        assert "backfill" in note and "pid " in note
    assert (tmp_path / LOCK_FILENAME).read_text() == ""


def test_a_second_command_is_refused_at_once_and_told_who_holds_it(tmp_path) -> None:
    slept: list[float] = []
    with pipeline_lock(tmp_path, command="run"):
        with pytest.raises(PipelineBusy) as refused:
            with pipeline_lock(tmp_path, command="backfill", sleep=slept.append):
                pytest.fail("the second command must not run")
    message = str(refused.value)
    assert message.startswith("backfill did not start")
    assert "(run, started " in message
    assert slept == [], "a typed command is told at once, not made to wait"


def test_the_lock_is_free_again_after_the_block_raises(tmp_path) -> None:
    with pytest.raises(ValueError):
        with pipeline_lock(tmp_path, command="run"):
            raise ValueError("pipeline failed")
    with pipeline_lock(tmp_path, command="backfill"):
        pass


def test_a_waiting_command_starts_when_the_holder_finishes(tmp_path, caplog) -> None:
    holder = pipeline_lock(tmp_path, command="export-market-results")
    holder.__enter__()
    waits: list[float] = []

    def release_on_second_look(seconds: float) -> None:
        waits.append(seconds)
        if len(waits) == 2:
            holder.__exit__(None, None, None)

    ran = False
    with caplog.at_level("WARNING"):
        with pipeline_lock(
            tmp_path, command="run", wait_seconds=600, sleep=release_on_second_look
        ):
            ran = True
    assert ran and waits == [run_lock.POLL_SECONDS] * 2
    # Announced once, not on every look.
    assert caplog.text.count("waiting up to 600s") == 1
    assert "export-market-results" in caplog.text


def test_a_wait_that_runs_out_fails_and_says_how_long_it_waited(tmp_path) -> None:
    # The deadline is set at 0; the lock is looked at again at 300 (still
    # inside the wait, so it sleeps) and at 601 (past it, so it gives up).
    clock = iter([0.0, 300.0, 601.0])
    slept: list[float] = []
    with pipeline_lock(tmp_path, command="backfill"):
        with pytest.raises(PipelineBusy, match="after waiting 600s"):
            with pipeline_lock(
                tmp_path,
                command="run",
                wait_seconds=600,
                sleep=slept.append,
                monotonic=lambda: next(clock),
            ):
                pytest.fail("must not run while the lock is held")
    assert slept == [run_lock.POLL_SECONDS], "one wait inside the deadline, then stop"


def test_a_killed_holder_leaves_no_stale_lock(tmp_path) -> None:
    """flock dies with the process: nothing to clean up after a crash."""
    script = textwrap.dedent(
        """
        import os, sys
        from betfair_results_downloader.run_lock import pipeline_lock
        with pipeline_lock(sys.argv[1], command="run"):
            os._exit(1)  # no cleanup, as after a kill
        """
    )
    src = Path(run_lock.__file__).resolve().parents[1]
    done = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path)],
        env={"PYTHONPATH": str(src)},
        timeout=60,
    )
    assert done.returncode == 1
    assert "run" in (tmp_path / LOCK_FILENAME).read_text(), "the note outlives it"
    with pipeline_lock(tmp_path, command="backfill"):
        pass


def test_without_fcntl_the_command_runs_unlocked_and_says_so(
    tmp_path, monkeypatch, caplog
) -> None:
    monkeypatch.setattr(run_lock, "fcntl", None)
    ran = False
    with caplog.at_level("WARNING"):
        with pipeline_lock(tmp_path, command="run"):
            ran = True
    assert ran and "runs without the pipeline lock" in caplog.text


def test_a_filesystem_that_refuses_flock_does_not_stop_the_run(
    tmp_path, monkeypatch, caplog
) -> None:
    def refuse(_handle, _flags):
        raise OSError(45, "Operation not supported")

    monkeypatch.setattr(run_lock.fcntl, "flock", refuse)
    ran = False
    with caplog.at_level("WARNING"):
        with pipeline_lock(tmp_path, command="run"):
            ran = True
    assert ran and "runs without the pipeline lock" in caplog.text


def test_a_lock_file_that_cannot_be_opened_does_not_stop_the_run(
    tmp_path, caplog
) -> None:
    # A directory where the lock file should be: open() fails.
    (tmp_path / LOCK_FILENAME).mkdir()
    ran = False
    with caplog.at_level("WARNING"):
        with pipeline_lock(tmp_path, command="backfill-commission"):
            ran = True
    assert ran and "runs without the pipeline lock" in caplog.text


# -----------------------------
# Wiring: the pipeline
# -----------------------------


def test_a_malformed_paths_section_is_still_a_failed_run_not_a_traceback() -> None:
    # The results directory is resolved to find the lock; a bad value must
    # reach the same failed RunResult it did before the lock existed.
    result = runner._run_pipeline(
        {"paths": "oops", "user": {}}, ScheduleConfig(), FROM_DT, TO_DT
    )
    assert (result.ok, result.status) == (False, "failed")
    assert result.message.startswith("Scheduled pipeline failed:")


def test_a_pipeline_refused_by_the_lock_fails_without_touching_betfair(
    tmp_path,
) -> None:
    with (
        pipeline_lock(tmp_path, command="backfill-commission"),
        patch.object(runner, "build_api_client") as login,
    ):
        result = runner._run_pipeline(
            _creds(tmp_path), ScheduleConfig(), FROM_DT, TO_DT, command="backfill"
        )
    assert (result.ok, result.status) == (False, "failed")
    assert result.message.startswith("backfill did not start")
    assert "backfill-commission" in result.message
    login.assert_not_called()
    assert sorted(p.name for p in tmp_path.iterdir()) == [LOCK_FILENAME]


def test_the_pipeline_holds_the_lock_while_it_runs_and_releases_it(tmp_path) -> None:
    seen: list[str] = []

    def inner(*_args, **_kwargs):
        seen.append((tmp_path / LOCK_FILENAME).read_text())
        with pytest.raises(PipelineBusy):
            with pipeline_lock(tmp_path, command="export-market-results"):
                pass
        return runner.RunResult(ok=True, status="success", message="done")

    with patch.object(runner, "_run_pipeline_inner", side_effect=inner):
        result = runner._run_pipeline(
            _creds(tmp_path), ScheduleConfig(), FROM_DT, TO_DT
        )
    assert result.status == "success" and "(run, started " in seen[0]
    with pipeline_lock(tmp_path, command="backfill"):
        pass


def test_the_scheduled_run_waits_and_a_backfill_does_not(tmp_path) -> None:
    done = runner.RunResult(ok=True, status="success", message="done")
    with (
        patch.object(runner, "_run_pipeline", return_value=done) as pipeline,
        patch.object(runner, "_preflight_disk", return_value=(True, None)),
        patch.object(
            runner, "compute_backfill_window", return_value=(FROM_DT, TO_DT, "gap")
        ),
        patch.object(runner, "upsert_schedule_state", return_value=False),
        patch.object(runner, "write_today_success_marker"),
        patch.object(runner, "append_run_history"),
    ):
        runner.run_scheduled(_creds(tmp_path), ScheduleConfig())
        scheduled = pipeline.call_args.kwargs
        runner.run_backfill(
            _creds(tmp_path), ScheduleConfig(), date(2026, 11, 1), date(2026, 11, 2)
        )
        backfill = pipeline.call_args.kwargs
    assert scheduled == {
        "command": "run",
        "lock_wait_seconds": runner.SCHEDULED_LOCK_WAIT_SECONDS,
    }
    assert backfill == {"command": "backfill"}


def test_a_refused_scheduled_run_is_recorded_as_failed(tmp_path) -> None:
    history: list[dict] = []
    with (
        pipeline_lock(tmp_path, command="backfill"),
        patch.object(runner, "SCHEDULED_LOCK_WAIT_SECONDS", 0.0),
        patch.object(runner, "_preflight_disk", return_value=(True, None)),
        patch.object(
            runner, "compute_backfill_window", return_value=(FROM_DT, TO_DT, "gap")
        ),
        patch.object(runner, "upsert_schedule_state") as state,
        patch.object(runner, "write_today_success_marker") as marker,
        patch.object(
            runner,
            "append_run_history",
            side_effect=lambda _d, rec: history.append(rec),
        ),
    ):
        result = runner.run_scheduled(_creds(tmp_path), ScheduleConfig())
    assert not result.ok
    assert history[0]["status"] == "failed"
    assert "run did not start" in history[0]["message"]
    # A run that never started confirms nothing: no checkpoint, no marker.
    state.assert_not_called()
    marker.assert_not_called()


# -----------------------------
# Wiring: the CLI
# -----------------------------


def _use_creds(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        "betfair_results_downloader.__main__._load_creds_and_schedule",
        lambda validate=False: (_creds(tmp_path), ScheduleConfig()),
    )


def test_backfill_commission_is_refused_before_it_logs_in(
    tmp_path, capsys, monkeypatch
) -> None:
    _use_creds(monkeypatch, tmp_path)
    with (
        pipeline_lock(tmp_path, command="run"),
        patch("betfair_results_downloader.scheduler.auth.build_api_client") as login,
        patch(
            "betfair_results_downloader.commission.update_market_commission"
        ) as update,
    ):
        code = main(
            ["backfill-commission", "--from", "2026-11-01", "--to", "2026-11-02"]
        )
    assert code == 1
    out = capsys.readouterr().out
    assert "FAIL: backfill-commission did not start" in out and "(run, started" in out
    login.assert_not_called()
    update.assert_not_called()


def test_backfill_commission_updates_the_store_under_the_lock(
    tmp_path, monkeypatch
) -> None:
    _use_creds(monkeypatch, tmp_path)
    notes: list[str] = []

    def update(**_kwargs):
        notes.append((tmp_path / LOCK_FILENAME).read_text())
        return MagicMock(message="ok")

    with (
        patch(
            "betfair_results_downloader.scheduler.auth.build_api_client",
            return_value=MagicMock(),
        ),
        patch(
            "betfair_results_downloader.commission.update_market_commission",
            side_effect=update,
        ),
    ):
        code = main(
            ["backfill-commission", "--from", "2026-11-01", "--to", "2026-11-02"]
        )
    assert code == 0 and "(backfill-commission, started " in notes[0]
    assert (tmp_path / LOCK_FILENAME).read_text() == ""


def test_export_market_results_is_refused_while_a_run_holds_the_lock(
    tmp_path, capsys, monkeypatch
) -> None:
    _use_creds(monkeypatch, tmp_path)
    (tmp_path / "cleared_orders_cleaned.csv").write_text(
        "betId,marketId,eventTypeId,settledDate,profit,evt_eventName,mkt_marketName\n"
        "1,1.100,1,2026-11-14T07:00:00.000Z,5.0,A v B,Correct Score\n"
    )
    with pipeline_lock(tmp_path, command="run"):
        assert main(["export-market-results"]) == 1
    assert "FAIL: export-market-results did not start" in capsys.readouterr().out
    assert not (tmp_path / FEED_FILENAME).exists()

    assert main(["export-market-results"]) == 0
    assert (tmp_path / FEED_FILENAME).exists()

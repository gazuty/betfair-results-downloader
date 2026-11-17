"""
Hourly retries: when the retry job runs the pipeline, when it stops, and
what the alerts say about it.
"""

from __future__ import annotations

import json
import plistlib
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from betfair_results_downloader import __main__ as cli
from betfair_results_downloader.__main__ import main
from betfair_results_downloader.config import ScheduleConfig
from betfair_results_downloader.run_lock import pipeline_lock
from betfair_results_downloader.scheduler import runner
from betfair_results_downloader.scheduler.installers import launchd
from betfair_results_downloader.scheduler.retry import (
    MAX_FAILURES_IN_A_ROW,
    decide_retry,
    failures_in_a_row,
)
from betfair_results_downloader.scheduler.runner import RunResult

FROM_DT = datetime(2026, 11, 17, 6, 0, tzinfo=timezone.utc)
TO_DT = datetime(2026, 11, 17, 8, 0, tzinfo=timezone.utc)

OK = {"status": "success", "retry_wanted": False}
BAD = {"status": "success", "retry_wanted": True}  # e.g. the feed was not rebuilt
FAILED = {"status": "failed", "retry_wanted": True}
BACKFILL = {"mode": "backfill", "status": "success"}
BEFORE_THE_FEATURE = {"status": "failed"}  # no retry_wanted key at all


def _history(tmp_path: Path, records: list) -> Path:
    lines = [r if isinstance(r, str) else json.dumps(r) for r in records]
    (tmp_path / "run_history.jsonl").write_text("".join(line + "\n" for line in lines))
    return tmp_path


# -----------------------------
# The count
# -----------------------------


@pytest.mark.parametrize(
    "records,expected",
    [
        ([], 0),
        ([OK], 0),
        ([FAILED], 1),
        ([OK, BAD, FAILED], 2),
        # A success in between ends the run of failures.
        ([FAILED, FAILED, OK, FAILED], 1),
        # A manual backfill neither repairs a scheduled failure nor counts.
        ([FAILED, BACKFILL, FAILED, BACKFILL], 2),
        # History from before the feature never asked for a retry.
        ([BEFORE_THE_FEATURE, BEFORE_THE_FEATURE], 0),
        ([BEFORE_THE_FEATURE, FAILED], 1),
        # A torn line from a crash mid-append is not an attempt.
        ([FAILED, "{not json", "", FAILED], 2),
        # Only an explicit true counts.
        ([{"status": "failed", "retry_wanted": "yes"}], 0),
    ],
)
def test_failures_are_counted_back_from_the_newest_attempt(
    tmp_path, records, expected
) -> None:
    assert failures_in_a_row(_history(tmp_path, records)) == expected


def test_no_history_means_nothing_to_retry(tmp_path) -> None:
    assert failures_in_a_row(tmp_path) == 0
    assert decide_retry(tmp_path).run is False


def test_an_unreadable_history_is_not_taken_as_a_failure(tmp_path, caplog) -> None:
    (tmp_path / "run_history.jsonl").mkdir()
    with caplog.at_level("WARNING"):
        assert failures_in_a_row(tmp_path) == 0
    assert "no retry is pending" in caplog.text


@pytest.mark.parametrize("failures", range(1, MAX_FAILURES_IN_A_ROW))
def test_it_retries_while_fewer_than_six_have_failed_in_a_row(
    tmp_path, failures
) -> None:
    decision = decide_retry(_history(tmp_path, [OK] + [FAILED] * failures))
    assert (decision.run, decision.failures) == (True, failures)


@pytest.mark.parametrize("failures", [MAX_FAILURES_IN_A_ROW, MAX_FAILURES_IN_A_ROW + 3])
def test_it_stops_at_six_in_a_row_and_stays_stopped(tmp_path, failures) -> None:
    decision = decide_retry(_history(tmp_path, [FAILED] * failures))
    assert decision.run is False
    assert f"{failures} failures in a row" in decision.reason


def test_a_success_re_arms_the_retries(tmp_path) -> None:
    history = [FAILED] * MAX_FAILURES_IN_A_ROW + [OK, FAILED]
    assert decide_retry(_history(tmp_path, history)).run is True


def test_the_limit_is_six() -> None:
    assert MAX_FAILURES_IN_A_ROW == 6


# -----------------------------
# Which runs ask for a retry
# -----------------------------


def _steps(status=None, commission=None, feed=((), True)):
    return (
        patch.object(runner, "_refresh_market_status", return_value=status),
        patch.object(runner, "_refresh_market_commission", return_value=commission),
        patch.object(runner, "_refresh_racecard", return_value=None),
        patch.object(
            runner, "_publish_market_results", return_value=(list(feed[0]), feed[1])
        ),
    )


def _quiet_pipeline(tmp_path, *patches) -> RunResult:
    """A run whose download returns nothing, with the given steps patched."""
    from betfair_results_downloader.downloader_core import DownloadResult

    creds = {"betfair": {}, "user": {}, "paths": {"results_csv_dir": str(tmp_path)}}
    with (
        patch.object(runner, "build_api_client"),
        patch(
            "betfair_results_downloader.downloader_core.fetch_cleared_orders_df_range",
            return_value=DownloadResult(
                attempted=True, rows_downloaded=0, message="none"
            ),
        ),
        patches[0],
        patches[1],
        patches[2],
        patches[3],
    ):
        return runner._run_pipeline(creds, ScheduleConfig(), FROM_DT, TO_DT)


def test_a_clean_run_does_not_ask(tmp_path) -> None:
    assert _quiet_pipeline(tmp_path, *_steps()).retry_wanted is False


@pytest.mark.parametrize(
    "steps",
    [
        _steps(status="⚠️ Market settlement status check failed"),
        _steps(commission="⚠️ Commission read failed"),
        _steps(feed=(["⚠️ Results feed not rebuilt"], False)),
    ],
    ids=["status", "commission", "feed-not-rebuilt"],
)
def test_a_successful_run_without_fresh_data_asks(tmp_path, steps) -> None:
    result = _quiet_pipeline(tmp_path, *steps)
    assert (result.ok, result.status, result.retry_wanted) == (True, "success", True)


def test_a_warning_a_retry_cannot_fix_does_not_ask(tmp_path) -> None:
    # A market stuck without commission: the feed was rebuilt, it just warns.
    steps = list(_steps(feed=(["⚠️ Results feed: stuck"], True)))
    steps[2] = patch.object(
        runner, "_refresh_racecard", return_value="⚠️ Racecard capture failed"
    )
    result = _quiet_pipeline(tmp_path, *steps)
    assert "Racecard capture failed" in result.message and "stuck" in result.message
    assert result.retry_wanted is False


def test_a_failed_login_and_a_busy_lock_ask(tmp_path) -> None:
    creds = {"betfair": {}, "user": {}, "paths": {"results_csv_dir": str(tmp_path)}}
    with patch.object(
        runner, "build_api_client", side_effect=RuntimeError("cert expired")
    ):
        failed = runner._run_pipeline(creds, ScheduleConfig(), FROM_DT, TO_DT)
    assert (failed.ok, failed.retry_wanted) == (False, True)
    with pipeline_lock(tmp_path, command="backfill"):
        busy = runner._run_pipeline(creds, ScheduleConfig(), FROM_DT, TO_DT)
    assert (busy.ok, busy.retry_wanted) == (False, True)


def _scheduled(tmp_path, result: RunResult, state_written: bool = True) -> dict:
    """run_scheduled around a canned pipeline result; returns the history record."""
    history: list[dict] = []
    creds = {"paths": {"results_csv_dir": str(tmp_path)}, "azure_sql": {"server": "x"}}
    with (
        patch.object(runner, "_run_pipeline", return_value=result),
        patch.object(runner, "_preflight_disk", return_value=(True, None)),
        patch.object(
            runner, "compute_backfill_window", return_value=(FROM_DT, TO_DT, "gap")
        ),
        patch.object(runner, "upsert_schedule_state", return_value=state_written),
        patch.object(runner, "write_today_success_marker"),
        patch.object(
            runner, "append_run_history", side_effect=lambda _d, r: history.append(r)
        ),
    ):
        runner.run_scheduled(creds, ScheduleConfig())
    return history[0]


def test_the_history_records_whether_each_attempt_asked(tmp_path) -> None:
    clean = RunResult(ok=True, status="success", message="done")
    assert _scheduled(tmp_path, clean)["retry_wanted"] is False
    stale = RunResult(ok=True, status="success", message="warned", retry_wanted=True)
    assert _scheduled(tmp_path, stale)["retry_wanted"] is True


def test_a_run_downgraded_after_the_pipeline_returned_asks_too(tmp_path) -> None:
    # The Azure checkpoint was not written: success becomes partial.
    clean = RunResult(ok=True, status="success", message="done")
    record = _scheduled(tmp_path, clean, state_written=False)
    assert (record["status"], record["retry_wanted"]) == ("partial", True)


def test_a_run_refused_for_disk_space_asks(tmp_path) -> None:
    history: list[dict] = []
    with (
        patch.object(
            runner, "_preflight_disk", return_value=(False, "Disk critically low")
        ),
        patch.object(
            runner, "append_run_history", side_effect=lambda _d, r: history.append(r)
        ),
    ):
        result = runner.run_scheduled({"paths": {}}, ScheduleConfig())
    assert result.retry_wanted is True and history[0]["retry_wanted"] is True


# -----------------------------
# The messages stay short
# -----------------------------


class APIError(Exception):
    pass


def test_a_step_failure_is_short_in_the_message_and_whole_in_the_log(
    tmp_path, caplog
) -> None:
    huge = APIError(
        "SportsAPING/v1.0/listMarketBook \nParams: {'marketIds': ["
        + ", ".join(f"'1.2655{n:05d}'" for n in range(400))
        + "]} \nError: {'code': -32099, 'message': 'ANGX-0006', 'data': "
        "{'APINGException': {'errorCode': 'UNEXPECTED_ERROR'}}}"
    )
    with (
        patch(
            "betfair_results_downloader.market_status.update_market_status",
            side_effect=huge,
        ),
        caplog.at_level("WARNING"),
    ):
        warning = runner._refresh_market_status(object(), tmp_path, None, None)
    assert warning == (
        "⚠️ Market settlement status check failed (APIError: "
        "SportsAPING/v1.0/listMarketBook returned UNEXPECTED_ERROR (ANGX-0006)); the "
        "report will treat this window's markets as fully settled until the next run."
    )
    # Nothing is lost: the whole error is in the log.
    assert (
        "'1.265500399'" in caplog.text
        and "Full error for the warning above" in caplog.text
    )


# -----------------------------
# CLI: run and retry
# -----------------------------


@pytest.fixture
def posted(monkeypatch) -> list[str]:
    sent: list[str] = []
    monkeypatch.setattr(
        cli, "_post_to_slack", lambda text, creds=None: sent.append(text) or 0
    )
    return sent


@pytest.fixture
def creds_ok(monkeypatch, tmp_path) -> Path:
    creds = {
        "paths": {"results_csv_dir": str(tmp_path)},
        "schedule": {"log_dir": str(tmp_path)},
    }
    monkeypatch.setattr(
        cli,
        "_load_creds_and_schedule",
        lambda *a, **k: (creds, ScheduleConfig(log_dir=str(tmp_path))),
    )
    return tmp_path


def _result(monkeypatch, result: RunResult) -> list[int]:
    calls: list[int] = []

    def fake(*_a, **_k):
        calls.append(1)
        return result

    monkeypatch.setattr(
        "betfair_results_downloader.scheduler.runner.run_scheduled", fake
    )
    return calls


def _retry_job(monkeypatch, installed: bool) -> None:
    class _Installer:
        def retry_status(self):
            return {"installed": installed, "loaded": installed}

    monkeypatch.setattr(
        "betfair_results_downloader.scheduler.installers.get_installer",
        lambda: _Installer(),
    )


STALE = RunResult(
    ok=True,
    status="success",
    message="Scheduled run complete. ⚠️ Results feed not rebuilt",
    retry_wanted=True,
)
CLEAN = RunResult(
    ok=True, status="success", message="Scheduled run complete: 577 rows."
)


def test_a_first_failure_says_it_will_be_retried_on_the_hour(
    posted, creds_ok, monkeypatch
) -> None:
    _retry_job(monkeypatch, installed=True)
    _result(monkeypatch, STALE)
    assert main(["run"]) == 0
    assert len(posted) == 1
    assert "succeeded with a warning" in posted[0]
    assert posted[0].endswith(
        "Failure 1 of 6 in a row; it will be retried on the hour."
    )


def test_without_the_retry_job_the_alert_does_not_promise_a_retry(
    posted, creds_ok, monkeypatch
) -> None:
    _retry_job(monkeypatch, installed=False)
    _result(
        monkeypatch,
        RunResult(ok=False, status="failed", message="auth", retry_wanted=True),
    )
    assert main(["run"]) == 1
    assert "retry job is not installed (schedule install-retry)" in posted[0]
    assert "retried on the hour" not in posted[0]


def test_the_sixth_failure_in_a_row_says_retries_have_stopped(
    posted, creds_ok, monkeypatch
) -> None:
    _retry_job(monkeypatch, installed=True)
    _history(creds_ok, [FAILED] * 5)
    _result(
        monkeypatch,
        RunResult(ok=False, status="failed", message="auth", retry_wanted=True),
    )
    assert main(["retry"]) == 1
    assert "That is 6 failures in a row: hourly retries have stopped." in posted[0]


def test_a_success_after_failures_is_announced_once(
    posted, creds_ok, monkeypatch
) -> None:
    _retry_job(monkeypatch, installed=True)
    _history(creds_ok, [OK, FAILED, BAD])
    _result(monkeypatch, CLEAN)
    assert main(["retry"]) == 0
    assert posted == [
        ":white_check_mark: Betfair scheduled run recovered after 2 failed attempt(s)\n"
        "Scheduled run complete: 577 rows."
    ]


def test_an_ordinary_success_is_still_silent(posted, creds_ok, monkeypatch) -> None:
    _retry_job(monkeypatch, installed=True)
    _history(creds_ok, [OK])
    _result(monkeypatch, CLEAN)
    assert main(["run"]) == 0
    assert posted == []


def test_retry_does_nothing_when_the_last_run_delivered(
    posted, creds_ok, monkeypatch, capsys
) -> None:
    _history(creds_ok, [FAILED, OK])
    calls = _result(monkeypatch, CLEAN)
    assert main(["retry"]) == 0
    assert calls == [] and posted == []
    assert "No retry pending" in capsys.readouterr().out


def test_retry_runs_the_pipeline_when_the_last_run_asked(
    posted, creds_ok, monkeypatch
) -> None:
    _retry_job(monkeypatch, installed=True)
    _history(creds_ok, [OK, BAD])
    calls = _result(monkeypatch, STALE)
    assert main(["retry"]) == 0
    assert calls == [1]
    assert "Failure 2 of 6 in a row" in posted[0]


def test_retry_stands_down_after_six_failures_without_running_or_posting(
    posted, creds_ok, monkeypatch, capsys
) -> None:
    _history(creds_ok, [FAILED] * MAX_FAILURES_IN_A_ROW)
    calls = _result(monkeypatch, CLEAN)
    assert main(["retry"]) == 0
    assert calls == [] and posted == []
    assert "Not retrying: 6 failures in a row" in capsys.readouterr().out


def test_a_scheduled_run_still_happens_after_retries_have_stopped(
    posted, creds_ok, monkeypatch
) -> None:
    _retry_job(monkeypatch, installed=True)
    _history(creds_ok, [FAILED] * MAX_FAILURES_IN_A_ROW)
    calls = _result(monkeypatch, CLEAN)
    assert main(["run"]) == 0
    assert calls == [1]
    assert "recovered after 6 failed attempt(s)" in posted[0]


def test_a_run_that_raises_is_recorded_so_the_retry_knows(
    posted, creds_ok, monkeypatch
) -> None:
    _retry_job(monkeypatch, installed=True)

    def boom(*_a, **_k):
        raise RuntimeError("gap detection exploded " + "x" * 5000)

    monkeypatch.setattr(
        "betfair_results_downloader.scheduler.runner.run_scheduled", boom
    )
    assert main(["run"]) == 1
    assert failures_in_a_row(creds_ok) == 1
    assert len(posted[0]) < 600 and "truncated; full text in the log" in posted[0]
    assert "Failure 1 of 6 in a row" in posted[0]


def test_an_alert_is_capped_whatever_reaches_it(posted, creds_ok, monkeypatch) -> None:
    _retry_job(monkeypatch, installed=True)
    _result(
        monkeypatch,
        RunResult(ok=False, status="failed", message="m" * 20_000, retry_wanted=True),
    )
    assert main(["run"]) == 1
    assert len(posted[0]) < 3100 and posted[0].endswith(
        "(truncated; full text in the log)"
    )


def test_no_post_slack_silences_a_retry_too(posted, creds_ok, monkeypatch) -> None:
    _history(creds_ok, [FAILED])
    _result(
        monkeypatch,
        RunResult(ok=False, status="failed", message="x", retry_wanted=True),
    )
    assert main(["retry", "--no-post-slack"]) == 1
    assert posted == []


def test_retry_with_unloadable_credentials_exits_2_without_posting(
    posted, monkeypatch
) -> None:
    def refuse(*_a, **_k):
        print("FAIL: credentials file not found")
        raise SystemExit(2)

    monkeypatch.setattr(cli, "_load_creds_and_schedule", refuse)
    with pytest.raises(SystemExit) as stopped:
        main(["retry"])
    assert stopped.value.code == 2 and posted == []


# -----------------------------
# The launchd job
# -----------------------------


def _cfg() -> ScheduleConfig:
    return ScheduleConfig(primary_time="06:00", retry_times=("09:00", "19:00", "23:00"))


def test_the_retry_job_fires_on_every_hour_without_a_scheduled_run(tmp_path) -> None:
    plist = launchd.build_retry_plist(_cfg(), tmp_path, Path(sys.executable), tmp_path)
    assert plist["ProgramArguments"][-3:] == [
        "-m",
        "betfair_results_downloader",
        "retry",
    ]
    hours = [entry["Hour"] for entry in plist["StartCalendarInterval"]]
    assert hours == [h for h in range(24) if h not in (6, 9, 19, 23)]
    assert {entry["Minute"] for entry in plist["StartCalendarInterval"]} == {0}
    assert plist["KeepAlive"] is False and plist["RunAtLoad"] is False
    plistlib.dumps(plist)  # serialisable


def test_an_hour_with_a_scheduled_run_at_any_minute_is_left_to_that_run(
    tmp_path,
) -> None:
    cfg = ScheduleConfig(primary_time="06:45", retry_times=())
    plist = launchd.build_retry_plist(cfg, tmp_path, Path(sys.executable), tmp_path)
    assert 6 not in [entry["Hour"] for entry in plist["StartCalendarInterval"]]
    assert len(plist["StartCalendarInterval"]) == 23


def test_install_retry_writes_the_plist_and_replaces_a_loaded_copy(
    tmp_path, monkeypatch
) -> None:
    import subprocess

    target = tmp_path / "retry.plist"
    monkeypatch.setattr(launchd, "RETRY_PLIST_PATH", target)
    monkeypatch.setattr(launchd, "AGENTS_DIR", tmp_path)
    calls: list[list[str]] = []

    def fake_run(args, **_kw):
        calls.append(list(args))
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(launchd.subprocess, "run", fake_run)
    result = launchd.LaunchdInstaller().install_retry(
        schedule_cfg=_cfg(),
        repo_root=tmp_path,
        venv_python_path=Path(sys.executable),
        log_dir=tmp_path,
    )
    assert result["ok"] is True and "20 hours a day" in result["message"]
    assert [c[1] for c in calls] == ["bootout", "bootstrap"]
    assert calls[0][2].endswith(f"/{launchd.RETRY_LABEL}")
    assert plistlib.loads(target.read_bytes())["Label"] == "com.betfair.results.retry"


def test_install_retry_dry_run_and_uninstall(tmp_path, monkeypatch) -> None:
    import subprocess

    target = tmp_path / "retry.plist"
    monkeypatch.setattr(launchd, "RETRY_PLIST_PATH", target)
    monkeypatch.setattr(launchd, "AGENTS_DIR", tmp_path)
    monkeypatch.setattr(
        launchd.subprocess,
        "run",
        lambda args, **_kw: subprocess.CompletedProcess(args, 0, "", ""),
    )
    installer = launchd.LaunchdInstaller()
    dry = installer.install_retry(schedule_cfg=_cfg(), repo_root=tmp_path, dry_run=True)
    assert dry["ok"] and not target.exists()
    assert installer.retry_status() == {"installed": False, "loaded": False}
    assert "nothing to uninstall" in installer.uninstall_retry()["message"]
    target.write_bytes(b"x")
    assert installer.uninstall_retry()["ok"] and not target.exists()

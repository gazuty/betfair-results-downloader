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
        # A backfill refused for disk space is marked "kind", with no
        # retry_wanted: it must be ignored too, not read as a success.
        ([FAILED, {"status": "failed", "kind": "backfill"}], 1),
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
    monkeypatch.setattr(sys.modules[__name__], "_LOG_DIR", tmp_path)
    return tmp_path


# Where the stand-in for run_scheduled records its attempt; set by creds_ok.
_LOG_DIR: Path | None = None


def _result(monkeypatch, result: RunResult) -> list[int]:
    """
    Stand in for run_scheduled. Like the real one it records the attempt in
    the history, because the alerts are worded from the history as it
    stands after the run.
    """
    calls: list[int] = []

    def fake(*_a, **_k):
        calls.append(1)
        if _LOG_DIR is not None:
            with (_LOG_DIR / "run_history.jsonl").open("a") as fh:
                record = {"status": result.status, "retry_wanted": result.retry_wanted}
                fh.write(json.dumps(record) + "\n")
        return result

    monkeypatch.setattr(
        "betfair_results_downloader.scheduler.runner.run_scheduled", fake
    )
    return calls


def _retry_job(monkeypatch, installed: bool, loaded: bool = True) -> None:
    class _Installer:
        def retry_status(self):
            return {"installed": installed, "loaded": loaded and installed}

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
    assert "retry job is not installed and loaded (schedule install-retry)" in posted[0]
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


def test_append_run_history_reports_whether_it_wrote(tmp_path) -> None:
    from betfair_results_downloader.scheduler.state import append_run_history

    assert append_run_history(tmp_path, {"status": "success"}) is True
    blocked = tmp_path / "a-file"
    blocked.write_text("not a directory")
    assert append_run_history(blocked, {"status": "failed"}) is False
    assert append_run_history("", {"status": "failed"}) is False


def _blocked_log_dir(monkeypatch, tmp_path: Path, creds: dict) -> Path:
    """A log directory that cannot be written: a file stands where it should be."""
    blocked = tmp_path / "logs"
    blocked.write_text("not a directory")
    monkeypatch.setattr(
        cli,
        "_load_creds_and_schedule",
        lambda *a, **k: (creds, ScheduleConfig(log_dir=str(blocked))),
    )
    return blocked


def test_an_attempt_that_could_not_be_recorded_does_not_promise_a_retry(
    posted, monkeypatch, tmp_path
) -> None:
    """
    Real run_scheduled. The retry job reads only the history, so when the
    record cannot be written the alert must say no retry will follow.
    """
    _retry_job(monkeypatch, installed=True)
    creds = {"paths": {"results_csv_dir": str(tmp_path / "results")}}
    blocked = _blocked_log_dir(monkeypatch, tmp_path, creds)
    failed = RunResult(ok=False, status="failed", message="auth", retry_wanted=True)
    with (
        patch.object(runner, "_run_pipeline", return_value=failed),
        patch.object(runner, "_preflight_disk", return_value=(True, None)),
        patch.object(
            runner, "compute_backfill_window", return_value=(FROM_DT, TO_DT, "gap")
        ),
    ):
        assert main(["run"]) == 1
    assert "could not be written to run_history.jsonl" in posted[0]
    assert "Hourly retries stand down while the history cannot be written" in posted[0]
    assert "retried on the hour" not in posted[0]
    assert "Failure 1 of 6" not in posted[0]
    assert decide_retry(blocked).run is False


def test_a_run_that_raises_and_cannot_be_recorded_says_so_too(
    posted, monkeypatch, tmp_path
) -> None:
    _retry_job(monkeypatch, installed=True)
    _blocked_log_dir(monkeypatch, tmp_path, {"paths": {}})

    def boom(*_a, **_k):
        raise RuntimeError("gap detection exploded")

    monkeypatch.setattr(
        "betfair_results_downloader.scheduler.runner.run_scheduled", boom
    )
    assert main(["run"]) == 1
    assert "could not be written to run_history.jsonl" in posted[0]
    assert "retried on the hour" not in posted[0]


def _real_retry(monkeypatch, log_dir: Path, results: Path, **patches):
    """main(["retry"]) through the real run_scheduled, with its outside world stubbed."""
    creds = {"paths": {"results_csv_dir": str(results)}}
    monkeypatch.setattr(
        cli,
        "_load_creds_and_schedule",
        lambda *a, **k: (creds, ScheduleConfig(log_dir=str(log_dir))),
    )
    pipeline = patches.pop(
        "pipeline", RunResult(ok=True, status="success", message="done")
    )
    window = patches.pop("window", None)

    def compute_window(*_a, **_k):
        if window:
            window()
        return (FROM_DT, TO_DT, "gap")

    with (
        patch.object(runner, "_preflight_disk", return_value=(True, None)),
        patch.object(runner, "compute_backfill_window", side_effect=compute_window),
        patch.object(runner, "upsert_schedule_state", return_value=False),
        patch.object(runner, "write_today_success_marker"),
        patch.object(runner, "_run_pipeline", return_value=pipeline) as ran,
    ):
        code = main(["retry"])
    return code, ran


def _records(log_dir: Path) -> list[dict]:
    lines = (log_dir / "run_history.jsonl").read_text().splitlines()
    return [json.loads(line) for line in lines]


def test_a_retry_claims_its_attempt_before_the_pipeline_runs(
    posted, monkeypatch, tmp_path
) -> None:
    _retry_job(monkeypatch, installed=True)
    (tmp_path / "logs").mkdir()
    log_dir = _history(tmp_path / "logs", [FAILED])
    code, ran = _real_retry(monkeypatch, log_dir, tmp_path / "results")
    assert code == 0 and ran.call_count == 1
    records = _records(log_dir)
    assert [r.get("phase") for r in records[-2:]] == ["started", None]
    assert records[-2]["kind"] == records[-1]["kind"] == "retry"
    assert records[-1]["retry_wanted"] is False
    # The claim and its outcome are one attempt, and it succeeded.
    assert failures_in_a_row(log_dir) == 0


def test_a_retry_that_cannot_claim_its_attempt_does_not_run(
    posted, monkeypatch, tmp_path, capsys
) -> None:
    """
    The limit is counted from the history. A history that can be read but
    not appended to (here: read-only; a full disk behaves the same at the
    write) would let a pending record run the pipeline every hour with the
    count never moving. The claim is a real line flushed to disk: if it
    cannot be written, the pipeline does not run.
    """
    (tmp_path / "logs").mkdir()
    log_dir = _history(tmp_path / "logs", [FAILED, FAILED])
    history = log_dir / "run_history.jsonl"
    history.chmod(0o444)
    try:
        for _hour in range(3):
            code, ran = _real_retry(monkeypatch, log_dir, tmp_path / "results")
            assert code == 0 and ran.call_count == 0
    finally:
        history.chmod(0o644)
    assert posted == []
    assert "could not be counted against the limit of 6" in capsys.readouterr().out
    assert len(_records(log_dir)) == 2, "nothing was written, and nothing was run"


def _stale_first_look(monkeypatch) -> None:
    """The retry command's first look at the history still saw a failure."""
    from betfair_results_downloader.scheduler import retry as policy

    first = iter([policy.RetryDecision(True, 1, "Retrying: stale first look.")])
    real_decide = policy.decide_retry
    monkeypatch.setattr(
        policy, "decide_retry", lambda d: next(first, None) or real_decide(d)
    )


def test_a_retry_stands_down_if_another_attempt_delivered_before_it_got_the_lock(
    posted, monkeypatch, tmp_path, capsys
) -> None:
    """
    A scheduled run started at the same moment finishes and records its
    success while the retry is still starting. The retry looked at the
    history before that; it must look again once it holds the lock.
    """
    (tmp_path / "logs").mkdir()
    log_dir = _history(tmp_path / "logs", [FAILED, {"kind": "scheduled", **OK}])
    _stale_first_look(monkeypatch)
    code, ran = _real_retry(monkeypatch, log_dir, tmp_path / "results")
    assert code == 0 and ran.call_count == 0
    assert posted == []
    assert "No retry pending" in capsys.readouterr().out
    assert [r.get("phase") for r in _records(log_dir)] == [None, None], (
        "no claim written"
    )


@pytest.mark.parametrize(
    "records,expected",
    [
        # A claim and its outcome are one attempt.
        (
            [
                FAILED,
                {"kind": "retry", "phase": "started", "retry_wanted": True},
                {"kind": "retry", "status": "failed", "retry_wanted": True},
            ],
            2,
        ),
        # A claim with no outcome (the retry crashed, or its final record
        # could not be written) is the failure it stands for.
        ([FAILED, {"kind": "retry", "phase": "started", "retry_wanted": True}], 2),
        # A scheduled run recorded after a dangling claim is not its outcome.
        (
            [
                FAILED,
                {"kind": "retry", "phase": "started", "retry_wanted": True},
                {"kind": "scheduled", "status": "failed", "retry_wanted": True},
            ],
            3,
        ),
        # A successful retry ends the run of failures, claim and all.
        (
            [
                FAILED,
                {"kind": "retry", "phase": "started", "retry_wanted": True},
                {"kind": "retry", "status": "success", "retry_wanted": False},
            ],
            0,
        ),
    ],
    ids=["claim+failure", "dangling-claim", "claim-then-scheduled", "claim+success"],
)
def test_a_claim_and_its_outcome_count_as_one_attempt(
    tmp_path, records, expected
) -> None:
    assert failures_in_a_row(_history(tmp_path, records)) == expected


def test_five_failed_retries_after_a_failure_reach_the_limit(
    posted, monkeypatch, tmp_path
) -> None:
    """End to end through the real run_scheduled: the sixth failure stops it."""
    _retry_job(monkeypatch, installed=True)
    (tmp_path / "logs").mkdir()
    log_dir = _history(tmp_path / "logs", [{"kind": "scheduled", **FAILED}])
    failing = RunResult(ok=False, status="failed", message="auth", retry_wanted=True)
    runs = 0
    for _hour in range(8):
        code, ran = _real_retry(
            monkeypatch, log_dir, tmp_path / "results", pipeline=failing
        )
        runs += ran.call_count
    assert runs == 5, "five retries after the first failure make six in a row"
    assert failures_in_a_row(log_dir) == 6
    assert "That is 6 failures in a row: hourly retries have stopped." in posted[-1]
    assert len(posted) == 5


def test_a_scheduled_run_computes_its_window_after_it_has_the_lock(tmp_path) -> None:
    """
    A scheduled run that waited behind a retry must not download the window
    it worked out before waiting, which the retry has just covered. The
    window comes from the checkpoint, so it is computed under the lock.
    """
    results = tmp_path / "results"
    order: list[str] = []

    def window(*_a, **_k):
        try:
            with pipeline_lock(results, command="retry"):
                order.append("window computed with the lock free")
        except Exception:
            order.append("window computed under the lock")
        return (FROM_DT, TO_DT, "gap")

    def preflight(*_a, **_k):
        try:
            with pipeline_lock(results, command="retry"):
                order.append("preflight with the lock free")
        except Exception:
            order.append("preflight under the lock")
        return (True, None)

    done = RunResult(ok=True, status="success", message="done")
    with (
        patch.object(runner, "_preflight_disk", side_effect=preflight),
        patch.object(runner, "compute_backfill_window", side_effect=window),
        patch.object(runner, "upsert_schedule_state", return_value=False),
        patch.object(runner, "write_today_success_marker"),
        patch.object(runner, "_run_pipeline", return_value=done),
    ):
        runner.run_scheduled(
            {"paths": {"results_csv_dir": str(results)}},
            ScheduleConfig(log_dir=str(tmp_path)),
        )
    assert order == ["preflight under the lock", "window computed under the lock"]


def test_a_retry_that_is_no_longer_due_records_no_disk_failure(
    posted, monkeypatch, tmp_path, capsys
) -> None:
    """
    Another attempt delivers while the retry is starting, and the disk is
    critically low. The retry must look at the history before the disk
    check can record a failure, or it would turn a clean history back into
    a pending one and alert for nothing.
    """
    (tmp_path / "logs").mkdir()
    log_dir = _history(tmp_path / "logs", [FAILED, {"kind": "scheduled", **OK}])
    _stale_first_look(monkeypatch)
    creds = {"paths": {"results_csv_dir": str(tmp_path / "results")}}
    monkeypatch.setattr(
        cli,
        "_load_creds_and_schedule",
        lambda *a, **k: (creds, ScheduleConfig(log_dir=str(log_dir))),
    )
    with patch.object(
        runner, "_preflight_disk", return_value=(False, "Disk critically low")
    ) as preflight:
        assert main(["retry"]) == 0
    preflight.assert_not_called()
    assert posted == []
    assert failures_in_a_row(log_dir) == 0 and len(_records(log_dir)) == 2


def test_a_retry_refused_for_disk_space_is_one_counted_attempt(
    posted, monkeypatch, tmp_path
) -> None:
    _retry_job(monkeypatch, installed=True)
    (tmp_path / "logs").mkdir()
    log_dir = _history(tmp_path / "logs", [{"kind": "scheduled", **FAILED}])
    creds = {"paths": {"results_csv_dir": str(tmp_path / "results")}}
    monkeypatch.setattr(
        cli,
        "_load_creds_and_schedule",
        lambda *a, **k: (creds, ScheduleConfig(log_dir=str(log_dir))),
    )
    with patch.object(
        runner, "_preflight_disk", return_value=(False, "Disk critically low")
    ):
        assert main(["retry"]) == 1
    # The claim and the refusal are one attempt: two failures in a row now.
    assert failures_in_a_row(log_dir) == 2
    assert "Failure 2 of 6 in a row" in posted[0]


def test_a_scheduled_run_keeps_the_lock_until_its_result_is_recorded(tmp_path) -> None:
    """
    Releasing the lock before the history is written would let a retry take
    it, still see the old pending failure, and run the pipeline again.
    """
    results = tmp_path / "results"
    seen: list[str] = []

    def append(_log_dir, _record, **_kw):
        try:
            with pipeline_lock(results, command="retry"):
                seen.append("free")
        except Exception as exc:
            seen.append(type(exc).__name__)
        return True

    creds = {"paths": {"results_csv_dir": str(results)}}
    done = RunResult(ok=True, status="success", message="done")
    with (
        patch.object(runner, "_preflight_disk", return_value=(True, None)),
        patch.object(
            runner, "compute_backfill_window", return_value=(FROM_DT, TO_DT, "gap")
        ),
        patch.object(runner, "upsert_schedule_state", return_value=False),
        patch.object(runner, "write_today_success_marker"),
        patch.object(runner, "_run_pipeline", return_value=done) as ran,
        patch.object(runner, "append_run_history", side_effect=append),
    ):
        runner.run_scheduled(creds, ScheduleConfig(log_dir=str(tmp_path)))
    assert seen == ["PipelineBusy"]
    assert ran.call_args.kwargs == {"hold_lock": False}
    with pipeline_lock(results, command="backfill"):
        pass  # and it is released afterwards


def test_the_claim_is_flushed_to_disk(tmp_path, monkeypatch) -> None:
    from betfair_results_downloader.scheduler import state
    from betfair_results_downloader.scheduler.retry import claim_attempt

    synced: list[int] = []
    monkeypatch.setattr(state.os, "fsync", lambda fd: synced.append(fd))
    assert claim_attempt(_history(tmp_path, [FAILED])).run is True
    assert len(synced) == 1


def test_a_writable_history_with_a_pending_record_still_retries(tmp_path) -> None:
    assert decide_retry(_history(tmp_path, [FAILED])).run is True


def test_a_plist_that_is_installed_but_not_loaded_promises_no_retry(
    posted, creds_ok, monkeypatch
) -> None:
    _retry_job(monkeypatch, installed=True, loaded=False)
    _result(monkeypatch, STALE)
    main(["run"])
    assert "not installed and loaded" in posted[0] and "on the hour" not in posted[0]


def test_a_recovery_that_carries_a_warning_still_says_it_recovered(
    posted, creds_ok, monkeypatch
) -> None:
    # The recovering run has a warning a retry cannot fix (a low disk).
    _retry_job(monkeypatch, installed=True)
    _history(creds_ok, [FAILED, FAILED])
    warned = RunResult(
        ok=True, status="success", message="Run complete. ⚠️ Disk low: 8.0 GB free."
    )
    _result(monkeypatch, warned)
    assert main(["retry"]) == 0
    assert len(posted) == 1 and "succeeded with a warning" in posted[0]
    assert posted[0].endswith("Recovered after 2 failed attempt(s).")


def test_two_overlapping_attempts_announce_one_recovery(
    posted, creds_ok, monkeypatch
) -> None:
    """
    After a wake launchd can start the scheduled run and a retry together.
    Both start while the history says two failures; the first to finish
    records its success, and the second must count that, not what it saw
    when it started.
    """
    _retry_job(monkeypatch, installed=True)
    _history(creds_ok, [FAILED, FAILED])
    calls: list[int] = []

    def second_attempt(*_a, **_k):
        calls.append(1)
        with (creds_ok / "run_history.jsonl").open("a") as fh:
            fh.write(json.dumps(OK) + "\n")  # the other attempt, finishing first
            fh.write(json.dumps(OK) + "\n")  # this one
        return CLEAN

    monkeypatch.setattr(
        "betfair_results_downloader.scheduler.runner.run_scheduled", second_attempt
    )
    assert main(["run"]) == 0
    assert calls == [1] and posted == [], "the first attempt already announced it"


def test_a_retry_stands_down_while_another_command_holds_the_lock(
    posted, creds_ok, monkeypatch, capsys
) -> None:
    """Real run_scheduled, real lock: no pipeline, no record, no alert."""
    _history(creds_ok, [FAILED])
    creds = {"paths": {"results_csv_dir": str(creds_ok)}}
    monkeypatch.setattr(
        cli,
        "_load_creds_and_schedule",
        lambda *a, **k: (creds, ScheduleConfig(log_dir=str(creds_ok))),
    )
    with (
        pipeline_lock(creds_ok, command="run"),
        patch.object(runner, "_preflight_disk", return_value=(True, None)),
        patch.object(
            runner, "compute_backfill_window", return_value=(FROM_DT, TO_DT, "gap")
        ),
        patch.object(runner, "build_api_client") as login,
        patch.object(runner, "upsert_schedule_state") as state,
    ):
        assert main(["retry"]) == 0
    login.assert_not_called()
    state.assert_not_called()
    assert posted == []
    assert failures_in_a_row(creds_ok) == 1, "standing down is not a failure"
    assert "stands down" in capsys.readouterr().out


def test_a_scheduled_run_that_never_gets_the_lock_is_a_failure(creds_ok) -> None:
    """Only the retry stands down."""
    creds = {"paths": {"results_csv_dir": str(creds_ok)}}
    with (
        pipeline_lock(creds_ok, command="backfill"),
        patch.object(runner, "SCHEDULED_LOCK_WAIT_SECONDS", 0.0),
        patch.object(runner, "_preflight_disk", return_value=(True, None)),
        patch.object(
            runner, "compute_backfill_window", return_value=(FROM_DT, TO_DT, "gap")
        ),
    ):
        result = runner.run_scheduled(creds, ScheduleConfig(log_dir=str(creds_ok)))
    assert (result.ok, result.busy, result.retry_wanted) == (False, True, True)
    assert failures_in_a_row(creds_ok) == 1


def test_a_run_that_cannot_load_its_configuration_is_recorded_for_the_retry(
    posted, monkeypatch, tmp_path
) -> None:
    def refuse(*_a, **_k):
        print("FAIL: could not parse credentials.json: OSError: [Errno 11]")
        raise SystemExit(2)

    monkeypatch.setattr(cli, "_load_creds_and_schedule", refuse)
    monkeypatch.setattr(runner, "_resolve_repo_root", lambda: tmp_path)
    with pytest.raises(SystemExit):
        main(["run"])
    assert len(posted) == 1 and "could not parse credentials.json" in posted[0]
    assert failures_in_a_row(tmp_path / "outputs") == 1
    assert decide_retry(tmp_path / "outputs").run is True


# -----------------------------
# The launchd job
# -----------------------------


@pytest.fixture(autouse=True)
def _no_installed_scheduler_plist(monkeypatch, tmp_path):
    """The retry hours also read the installed scheduler plist; not this Mac's."""
    monkeypatch.setattr(launchd, "PLIST_PATH", tmp_path / "absent-scheduler.plist")


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


def test_the_retry_hours_follow_the_installed_schedule_not_only_the_config(
    tmp_path, monkeypatch
) -> None:
    # `schedule install --time 07:30` installed a time credentials.json does
    # not hold: the retry must stay out of that hour as well.
    installed = tmp_path / "scheduler.plist"
    installed.write_bytes(
        plistlib.dumps({"StartCalendarInterval": [{"Hour": 7, "Minute": 30}]})
    )
    monkeypatch.setattr(launchd, "PLIST_PATH", installed)
    plist = launchd.build_retry_plist(_cfg(), tmp_path, Path(sys.executable), tmp_path)
    hours = [entry["Hour"] for entry in plist["StartCalendarInterval"]]
    assert hours == [h for h in range(24) if h not in (6, 7, 9, 19, 23)]


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

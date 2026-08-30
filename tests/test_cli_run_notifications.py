"""
The scheduled `run` command is the unattended job. Every non-success outcome
must reach Slack and must not exit 0 -- a failure that only lands in
launchd.err.log is a failure nobody sees.
"""

from __future__ import annotations

import pytest

from betfair_results_downloader.__main__ import main
from betfair_results_downloader.scheduler.runner import RunResult


@pytest.fixture
def posted(monkeypatch) -> list[str]:
    sent: list[str] = []

    def fake_post(text: str, creds: dict | None = None) -> int:
        sent.append(text)
        return 0

    monkeypatch.setattr("betfair_results_downloader.__main__._post_to_slack", fake_post)
    return sent


@pytest.fixture
def creds_ok(monkeypatch):
    monkeypatch.setattr(
        "betfair_results_downloader.__main__._load_creds_and_schedule",
        lambda *a, **k: ({"paths": {"results_csv_dir": "/tmp"}}, object()),
    )


def _run_result(monkeypatch, result: RunResult) -> None:
    monkeypatch.setattr(
        "betfair_results_downloader.scheduler.runner.run_scheduled",
        lambda *a, **k: result,
    )


def test_unreadable_credentials_are_announced(posted, monkeypatch) -> None:
    def boom(*_args, **_kwargs):
        print("FAIL: could not parse credentials.json: OSError: [Errno 11] ...")
        raise SystemExit(2)

    monkeypatch.setattr(
        "betfair_results_downloader.__main__._load_creds_and_schedule", boom
    )

    with pytest.raises(SystemExit):
        main(["run"])

    assert len(posted) == 1
    assert "could not parse credentials.json" in posted[0]


def test_failed_run_is_announced_with_errors(posted, creds_ok, monkeypatch) -> None:
    _run_result(
        monkeypatch,
        RunResult(
            ok=False,
            status="failed",
            message="download blew up",
            errors=["betfair timeout", "second problem"],
        ),
    )

    exit_code = main(["run"])

    assert exit_code == 1
    assert len(posted) == 1
    assert "download blew up" in posted[0]
    assert "betfair timeout" in posted[0]
    assert "second problem" in posted[0]


def test_partial_run_does_not_exit_zero(posted, creds_ok, monkeypatch) -> None:
    """A partial run exiting 0 hides an Azure failure from launchd."""
    _run_result(
        monkeypatch,
        RunResult(ok=True, status="partial", message="checkpoint not written"),
    )

    exit_code = main(["run"])

    assert exit_code == 1
    assert len(posted) == 1
    assert "checkpoint not written" in posted[0]


def test_successful_run_is_silent(posted, creds_ok, monkeypatch) -> None:
    _run_result(
        monkeypatch,
        RunResult(ok=True, status="success", message="1,234 rows"),
    )

    assert main(["run"]) == 0
    assert posted == []


def test_no_post_slack_suppresses_the_announcement(
    posted, creds_ok, monkeypatch
) -> None:
    _run_result(monkeypatch, RunResult(ok=False, status="failed", message="nope"))

    assert main(["run", "--no-post-slack"]) == 1
    assert posted == []

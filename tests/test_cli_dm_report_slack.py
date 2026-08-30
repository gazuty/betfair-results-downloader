"""
Regression tests for --post-slack failure announcement.

The point of the flag is that a broken run is announced rather than failing
silently. Setup failures happen before the report is built, so they need
explicit handling: ``_load_creds_and_schedule`` prints its reason and raises
SystemExit, which would otherwise bypass the notifier entirely.
"""

from __future__ import annotations

import pytest

from betfair_results_downloader.__main__ import main


@pytest.fixture
def posted(monkeypatch) -> list[str]:
    sent: list[str] = []

    def fake_post(text: str) -> int:
        sent.append(text)
        return 0

    monkeypatch.setattr("betfair_results_downloader.__main__._post_to_slack", fake_post)
    return sent


def test_unreadable_credentials_is_announced_to_slack(posted, monkeypatch) -> None:
    """An unreadable credentials.json must still reach Slack, not just the log."""

    def boom(*_args, **_kwargs):
        print("FAIL: could not parse credentials.json: OSError: [Errno 11] ...")
        raise SystemExit(2)

    monkeypatch.setattr(
        "betfair_results_downloader.__main__._load_creds_and_schedule", boom
    )

    with pytest.raises(SystemExit):
        main(["dm-report", "--post-slack"])

    assert len(posted) == 1
    assert "Betfair DM report failed" in posted[0]
    assert "could not parse credentials.json" in posted[0]


def test_missing_results_dir_is_announced_to_slack(posted, monkeypatch) -> None:
    """A credentials file without paths.results_csv_dir must also announce."""
    monkeypatch.setattr(
        "betfair_results_downloader.__main__._load_creds_and_schedule",
        lambda *a, **k: ({"paths": {}}, None),
    )

    exit_code = main(["dm-report", "--post-slack"])

    assert exit_code == 2
    assert len(posted) == 1
    assert "results_csv_dir" in posted[0]


def test_setup_failure_stays_silent_without_the_flag(posted, monkeypatch) -> None:
    """Without --post-slack nothing is posted, however the run fails."""

    def boom(*_args, **_kwargs):
        print("FAIL: could not parse credentials.json: OSError: [Errno 11] ...")
        raise SystemExit(2)

    monkeypatch.setattr(
        "betfair_results_downloader.__main__._load_creds_and_schedule", boom
    )

    with pytest.raises(SystemExit):
        main(["dm-report"])

    assert posted == []

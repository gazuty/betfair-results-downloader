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


def test_invalid_at_timestamp_is_announced_to_slack(posted, monkeypatch) -> None:
    """A malformed --at is a config typo in a scheduled job; announce it."""
    monkeypatch.setattr(
        "betfair_results_downloader.__main__._load_creds_and_schedule",
        lambda *a, **k: ({"paths": {"results_csv_dir": "/tmp"}}, None),
    )

    exit_code = main(["dm-report", "--post-slack", "--at", "not-a-timestamp"])

    assert exit_code == 2
    assert len(posted) == 1
    assert "invalid --at datetime" in posted[0]


def test_malformed_schedule_config_is_announced_to_slack(posted, monkeypatch) -> None:
    """parse_schedule_config raises ValueError, not SystemExit; still announce."""

    def bad_config(*_args, **_kwargs):
        raise ValueError("invalid literal for int() with base 10: 'not-a-number'")

    monkeypatch.setattr(
        "betfair_results_downloader.__main__._load_creds_and_schedule", bad_config
    )

    with pytest.raises(SystemExit) as excinfo:
        main(["dm-report", "--post-slack"])

    assert excinfo.value.code == 2
    assert len(posted) == 1
    assert "could not load configuration" in posted[0]


def test_notifier_survives_the_broken_config_it_reports(monkeypatch) -> None:
    """_post_to_slack reloads config; a ValueError there must not crash it."""
    sent: list[str] = []

    def bad_config(*_args, **_kwargs):
        raise ValueError("bad schedule value")

    monkeypatch.setattr(
        "betfair_results_downloader.__main__._load_creds_and_schedule", bad_config
    )
    monkeypatch.setattr(
        "betfair_results_downloader.slack_notify.post_message",
        lambda text, creds=None, channel_override=None: sent.append(text) or "1.0",
    )

    from betfair_results_downloader.__main__ import _post_to_slack

    assert _post_to_slack("boom") == 0
    assert sent == ["boom"]

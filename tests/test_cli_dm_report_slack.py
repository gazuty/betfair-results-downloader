"""
Regression tests for --post-slack failure announcement.

The point of the flag is that a broken run is announced rather than failing
silently. Setup failures happen before the report is built, so they need
explicit handling: ``_load_creds_and_schedule`` prints its reason and raises
SystemExit, which would otherwise bypass the notifier entirely.
"""

from __future__ import annotations

import json

import pytest

from betfair_results_downloader.__main__ import main


def _raises_value_error(*_args, **_kwargs):
    raise ValueError("bad schedule value")


@pytest.fixture
def posted(monkeypatch) -> list[str]:
    sent: list[str] = []

    def fake_post(text: str, creds: dict | None = None) -> int:
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


def test_notifier_keeps_embedded_slack_creds_when_schedule_is_malformed(
    monkeypatch, tmp_path
) -> None:
    """
    Slack configured only via the credentials.json fallback must survive an
    unrelated malformed schedule value. Loading creds via the schedule-parsing
    path would discard the slack section and silence the notification.
    """
    captured: dict = {}
    creds_file = tmp_path / "credentials.json"
    creds_file.write_text(
        json.dumps({"slack": {"bot_token": "xoxb-test", "channel": "U1"}}),
        encoding="utf-8",
    )

    monkeypatch.setattr(
        "betfair_results_downloader.secrets.credentials_path", lambda: creds_file
    )
    monkeypatch.setattr(
        "betfair_results_downloader.secrets.load_credentials",
        lambda path: json.loads(creds_file.read_text(encoding="utf-8")),
    )
    # Would raise if the notifier still went through schedule parsing.
    monkeypatch.setattr(
        "betfair_results_downloader.__main__._load_creds_and_schedule",
        _raises_value_error,
    )
    monkeypatch.setattr(
        "betfair_results_downloader.slack_notify.post_message",
        lambda text, creds=None, channel_override=None: (
            captured.update(text=text, creds=creds) or "1.0"
        ),
    )

    from betfair_results_downloader.__main__ import _post_to_slack

    assert _post_to_slack("boom") == 0
    assert captured["text"] == "boom"
    assert captured["creds"]["slack"]["bot_token"] == "xoxb-test", (
        "the embedded slack section must reach post_message"
    )


def test_notifier_falls_back_to_empty_creds_when_credentials_unreadable(
    monkeypatch, tmp_path
) -> None:
    """An unreadable credentials.json leaves ~/.betfair/slack.json to carry it."""
    captured: dict = {}
    creds_file = tmp_path / "credentials.json"
    creds_file.write_text("{}", encoding="utf-8")

    def unreadable(_path):
        raise OSError(11, "Resource deadlock avoided")

    monkeypatch.setattr(
        "betfair_results_downloader.secrets.credentials_path", lambda: creds_file
    )
    monkeypatch.setattr(
        "betfair_results_downloader.secrets.load_credentials", unreadable
    )
    monkeypatch.setattr(
        "betfair_results_downloader.slack_notify.post_message",
        lambda text, creds=None, channel_override=None: (
            captured.update(creds=creds) or "1.0"
        ),
    )

    from betfair_results_downloader.__main__ import _post_to_slack

    assert _post_to_slack("boom") == 0
    assert captured["creds"] == {}


def test_malformed_paths_section_is_announced_to_slack(posted, monkeypatch) -> None:
    """A non-object `paths` must not raise AttributeError outside the handlers."""
    monkeypatch.setattr(
        "betfair_results_downloader.__main__._load_creds_and_schedule",
        lambda *a, **k: ({"paths": "not-an-object"}, None),
    )

    exit_code = main(["dm-report", "--post-slack"])

    assert exit_code == 2
    assert len(posted) == 1
    assert "results_csv_dir" in posted[0]


def test_successful_report_posts_with_already_loaded_creds(monkeypatch) -> None:
    """
    Post-load paths must reuse the loaded credentials rather than re-reading
    a cloud-backed credentials.json that may have been evicted mid-run.
    """
    captured: dict = {}
    loaded = {"paths": {"results_csv_dir": "/tmp"}, "slack": {"bot_token": "xoxb-y"}}

    monkeypatch.setattr(
        "betfair_results_downloader.__main__._load_creds_and_schedule",
        lambda *a, **k: (loaded, None),
    )

    def explode(_path):
        raise OSError(11, "Resource deadlock avoided")

    # Any re-read from disk would hit this and lose the slack section.
    monkeypatch.setattr("betfair_results_downloader.secrets.load_credentials", explode)
    monkeypatch.setattr(
        "betfair_results_downloader.slack_notify.post_message",
        lambda text, creds=None, channel_override=None: (
            captured.update(creds=creds) or "1.0"
        ),
    )

    from betfair_results_downloader.__main__ import _post_to_slack

    assert _post_to_slack("report body", loaded) == 0
    assert captured["creds"]["slack"]["bot_token"] == "xoxb-y"


def test_explicit_csv_does_not_require_credentials(
    tmp_path, capsys, monkeypatch
) -> None:
    """
    --csv is self-sufficient: results_dir is ignored downstream, so demanding
    credentials.json would require configuration the run never reads.

    (Carried over from work in progress on this command; the accompanying
    silent fallback to a hardcoded OneDrive path was deliberately dropped.)
    """
    csv_path = tmp_path / "cleared_orders_cleaned.csv"
    csv_path.write_text(
        "betId,eventTypeId,profit,settledDate\n1,7,5.0,2026-06-05T19:30:00Z\n",
        encoding="utf-8",
    )

    def fail_loader(*_args, **_kwargs):
        raise AssertionError("credentials must not be loaded when --csv is given")

    monkeypatch.setattr(
        "betfair_results_downloader.__main__._load_creds_and_schedule", fail_loader
    )

    exit_code = main(["dm-report", "--csv", str(csv_path)])

    assert exit_code == 0
    assert "Total profit" in capsys.readouterr().out


def test_explicit_csv_leaves_creds_unresolved_for_the_notifier(
    tmp_path, monkeypatch
) -> None:
    """
    With --csv no credentials are loaded, so the notifier must resolve them
    itself. Passing {} instead of None would suppress that lookup and break
    --csv --post-slack for anyone configuring Slack via credentials.json.
    """
    seen: dict = {}
    csv_path = tmp_path / "cleared_orders_cleaned.csv"
    csv_path.write_text(
        "betId,eventTypeId,profit,settledDate\n1,7,5.0,2026-06-05T19:30:00Z\n",
        encoding="utf-8",
    )

    def capture(text: str, creds: dict | None = None) -> int:
        seen["creds"] = creds
        return 0

    monkeypatch.setattr("betfair_results_downloader.__main__._post_to_slack", capture)

    exit_code = main(["dm-report", "--csv", str(csv_path), "--post-slack"])

    assert exit_code == 0
    assert seen["creds"] is None, "must stay None so the notifier resolves creds"


def test_partial_local_slack_config_does_not_shadow_credentials(monkeypatch) -> None:
    """
    A half-written ~/.betfair/slack.json must not silence alerting by
    shadowing a working credentials.json slack section.
    """
    from betfair_results_downloader import slack_notify

    monkeypatch.setattr(
        slack_notify, "_read_local_config", lambda: {"channel": "U-local"}
    )

    token, channel = slack_notify.load_slack_config(
        {"slack": {"bot_token": "xoxb-embedded", "channel": "U-embedded"}}
    )

    assert token == "xoxb-embedded", "embedded token must survive a partial local file"
    assert channel == "U-local", "local values still win where present"


def test_local_slack_config_overrides_credentials(monkeypatch) -> None:
    from betfair_results_downloader import slack_notify

    monkeypatch.setattr(
        slack_notify,
        "_read_local_config",
        lambda: {"bot_token": "xoxb-local", "channel": "U-local"},
    )

    token, channel = slack_notify.load_slack_config(
        {"slack": {"bot_token": "xoxb-embedded", "channel": "U-embedded"}}
    )

    assert (token, channel) == ("xoxb-local", "U-local")


def test_empty_local_values_do_not_blank_out_credentials(monkeypatch) -> None:
    from betfair_results_downloader import slack_notify

    monkeypatch.setattr(
        slack_notify, "_read_local_config", lambda: {"bot_token": "", "channel": None}
    )

    token, channel = slack_notify.load_slack_config(
        {"slack": {"bot_token": "xoxb-embedded", "channel": "U-embedded"}}
    )

    assert (token, channel) == ("xoxb-embedded", "U-embedded")


def test_local_config_is_not_silenced_by_a_stale_embedded_disable(monkeypatch) -> None:
    """
    A complete local config that never mentions `enabled` must not be switched
    off by a stale `enabled: false` in credentials.json. Before the merge, the
    local file was selected wholesale and this worked.
    """
    from betfair_results_downloader import slack_notify

    monkeypatch.setattr(
        slack_notify,
        "_read_local_config",
        lambda: {"bot_token": "xoxb-local", "channel": "U-local"},
    )

    token, channel = slack_notify.load_slack_config(
        {"slack": {"enabled": False, "bot_token": "xoxb-old", "channel": "U-old"}}
    )

    assert (token, channel) == ("xoxb-local", "U-local")


def test_explicit_local_disable_is_honoured(monkeypatch) -> None:
    from betfair_results_downloader import slack_notify

    monkeypatch.setattr(
        slack_notify,
        "_read_local_config",
        lambda: {"enabled": False, "bot_token": "xoxb-local", "channel": "U-local"},
    )

    with pytest.raises(slack_notify.SlackNotConfigured):
        slack_notify.load_slack_config({"slack": {"bot_token": "x", "channel": "y"}})


def test_embedded_disable_still_applies_without_a_local_file(monkeypatch) -> None:
    from betfair_results_downloader import slack_notify

    monkeypatch.setattr(slack_notify, "_read_local_config", lambda: None)

    with pytest.raises(slack_notify.SlackNotConfigured):
        slack_notify.load_slack_config(
            {"slack": {"enabled": False, "bot_token": "x", "channel": "y"}}
        )


@pytest.mark.parametrize("bad", [["a", "list"], "a string", 42, None])
def test_non_object_credentials_do_not_break_the_notifier(monkeypatch, bad) -> None:
    """
    A credentials.json with a non-object top level makes validation raise, and
    the failure path hands that value back to the notifier. It must still be
    able to announce the problem from the local file.
    """
    from betfair_results_downloader import slack_notify

    monkeypatch.setattr(
        slack_notify,
        "_read_local_config",
        lambda: {"bot_token": "xoxb-local", "channel": "U-local"},
    )

    assert slack_notify.load_slack_config(bad) == ("xoxb-local", "U-local")

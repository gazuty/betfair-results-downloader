"""CLI surface of ``racecard``, ``export-market-results`` and the racecard job install."""

from __future__ import annotations

import plistlib
from pathlib import Path
from unittest.mock import MagicMock, patch

from betfair_results_downloader.__main__ import main
from betfair_results_downloader.config import ScheduleConfig
from betfair_results_downloader.market_results import FEED_FILENAME, META_FILENAME
from betfair_results_downloader.racecard import RacecardResult
from betfair_results_downloader.scheduler.installers import launchd


def _creds(tmp_path: Path) -> dict:
    return {
        "betfair": {"username": "u", "password": "p", "app_key": "k"},
        "paths": {"results_csv_dir": str(tmp_path)},
        "user": {},
        "schedule": {},
    }


def _use_creds(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        "betfair_results_downloader.__main__._load_creds_and_schedule",
        lambda validate=False: (_creds(tmp_path), ScheduleConfig()),
    )


def test_racecard_captures_with_the_bet_markets_and_logs_out(
    tmp_path, capsys, monkeypatch
) -> None:
    _use_creds(monkeypatch, tmp_path)
    client = MagicMock()
    result = RacecardResult(
        captured=5, stored=7, truncated_slices=0, message="Racecard: ok"
    )
    with (
        patch(
            "betfair_results_downloader.scheduler.auth.build_api_client",
            return_value=client,
        ),
        patch(
            "betfair_results_downloader.racecard.update_racecard", return_value=result
        ) as update,
    ):
        assert main(["racecard"]) == 0

    assert "OK: Racecard: ok" in capsys.readouterr().out
    assert update.call_args.kwargs["cache_dir"] == tmp_path / ".cache"
    assert update.call_args.kwargs["bet_market_keys"] == set()
    client.logout.assert_called_once()


def test_racecard_failure_exits_1_without_slack(tmp_path, capsys, monkeypatch) -> None:
    _use_creds(monkeypatch, tmp_path)
    with (
        patch(
            "betfair_results_downloader.scheduler.auth.build_api_client",
            return_value=MagicMock(),
        ),
        patch(
            "betfair_results_downloader.racecard.update_racecard",
            side_effect=RuntimeError("catalogue down"),
        ),
        patch("betfair_results_downloader.__main__._post_to_slack") as slack,
    ):
        assert main(["racecard"]) == 1

    assert "catalogue down" in capsys.readouterr().out
    slack.assert_not_called()


def test_export_market_results_writes_the_feed(tmp_path, capsys, monkeypatch) -> None:
    _use_creds(monkeypatch, tmp_path)
    (tmp_path / "cleared_orders_cleaned.csv").write_text(
        "betId,marketId,eventTypeId,settledDate,profit,evt_eventName,mkt_marketName\n"
        "1,1.100,1,2026-11-14T07:00:00.000Z,5.0,A v B,Correct Score\n"
    )
    assert main(["export-market-results"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("OK: Results feed: 0 markets")  # commission unknown: held
    assert (tmp_path / FEED_FILENAME).exists()
    assert (tmp_path / META_FILENAME).exists()


def test_export_market_results_fails_without_a_canonical(
    tmp_path, capsys, monkeypatch
) -> None:
    _use_creds(monkeypatch, tmp_path)
    assert main(["export-market-results"]) == 1
    assert "results feed not rebuilt" in capsys.readouterr().out


def test_racecard_plist_runs_hourly_at_half_past(tmp_path: Path) -> None:
    plist = launchd.build_racecard_plist(tmp_path, Path("/py"), tmp_path / "logs")
    assert plist["Label"] == launchd.RACECARD_LABEL
    assert plist["ProgramArguments"] == [
        "/py",
        "-m",
        "betfair_results_downloader",
        "racecard",
    ]
    assert plist["StartCalendarInterval"] == [{"Minute": 30}]
    assert plist["KeepAlive"] is False and plist["RunAtLoad"] is False


def test_install_racecard_replaces_a_loaded_copy(tmp_path: Path, monkeypatch) -> None:
    calls: list[list[str]] = []

    class _Ok:
        returncode = 0
        stdout = ""
        stderr = ""

    monkeypatch.setattr(
        launchd.subprocess, "run", lambda cmd, **_k: calls.append(cmd) or _Ok()
    )
    target = tmp_path / "agents" / "racecard.plist"
    with (
        patch.object(launchd, "AGENTS_DIR", tmp_path / "agents"),
        patch.object(launchd, "RACECARD_PLIST_PATH", target),
    ):
        result = launchd.LaunchdInstaller().install_racecard(
            repo_root=tmp_path, venv_python_path=Path("/py"), log_dir=tmp_path
        )

    assert result["ok"]
    assert plistlib.loads(target.read_bytes())["Label"] == launchd.RACECARD_LABEL
    assert calls[0][:2] == ["launchctl", "bootout"]
    assert calls[1][:2] == ["launchctl", "bootstrap"]


def test_uninstall_racecard_removes_the_plist(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(launchd.subprocess, "run", lambda *a, **k: None)
    target = tmp_path / "racecard.plist"
    target.write_bytes(b"x")
    with patch.object(launchd, "RACECARD_PLIST_PATH", target):
        assert launchd.LaunchdInstaller().uninstall_racecard()["ok"]
    assert not target.exists()


class _FakeInstaller:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def install_racecard(self, **kwargs):
        self.calls.append("install_racecard")
        return {"ok": True, "message": "installed racecard"}

    def uninstall_racecard(self):
        self.calls.append("uninstall_racecard")
        return {"ok": True, "message": "uninstalled racecard"}

    def status(self):
        return {"installed": False, "message": "scheduler not installed"}

    def racecard_status(self):
        return {"installed": True, "loaded": True}


def test_schedule_dispatches_the_racecard_actions(
    tmp_path, capsys, monkeypatch
) -> None:
    _use_creds(monkeypatch, tmp_path)
    fake = _FakeInstaller()
    monkeypatch.setattr(
        "betfair_results_downloader.scheduler.installers.get_installer", lambda: fake
    )
    assert main(["schedule", "install-racecard"]) == 0
    assert main(["schedule", "uninstall-racecard"]) == 0
    assert fake.calls == ["install_racecard", "uninstall_racecard"]
    out = capsys.readouterr().out
    assert "installed racecard" in out and "uninstalled racecard" in out


def test_schedule_status_reports_the_racecard_job(
    tmp_path, capsys, monkeypatch
) -> None:
    _use_creds(monkeypatch, tmp_path)
    monkeypatch.setattr(
        "betfair_results_downloader.scheduler.installers.get_installer",
        lambda: _FakeInstaller(),
    )
    assert main(["schedule", "status"]) == 0
    assert "racecard job  : installed=True loaded=True" in capsys.readouterr().out

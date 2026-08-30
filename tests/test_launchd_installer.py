"""Tests for Phase 3.1: macOS launchd installer."""

from __future__ import annotations

import plistlib
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from betfair_results_downloader.config import ScheduleConfig
from betfair_results_downloader.scheduler.installers.launchd import (
    LABEL,
    LaunchdInstaller,
    _build_calendar_intervals,
    _parse_hh_mm,
    build_plist,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _cfg(**overrides) -> ScheduleConfig:
    defaults = dict(
        enabled=True,
        timezone="Australia/Sydney",
        primary_time="06:00",
        retry_times=("09:00", "19:00", "23:00"),
        publish_to_azure=True,
        allow_azure_publish=False,
        max_backfill_days=90,
        chunk_days=30,
        log_dir="",
    )
    defaults.update(overrides)
    return ScheduleConfig(**defaults)


# ---------------------------------------------------------------------------
# _parse_hh_mm
# ---------------------------------------------------------------------------


class TestParseHhMm:
    def test_valid_time(self) -> None:
        assert _parse_hh_mm("06:00") == (6, 0)
        assert _parse_hh_mm("23:59") == (23, 59)
        assert _parse_hh_mm("00:00") == (0, 0)

    def test_invalid_raises(self) -> None:
        with pytest.raises(Exception):
            _parse_hh_mm("not-a-time")  # no colon → ValueError from split logic

        with pytest.raises(Exception):
            _parse_hh_mm("06:00:00")  # extra segment → ValueError on too many parts


# ---------------------------------------------------------------------------
# _build_calendar_intervals
# ---------------------------------------------------------------------------


class TestBuildCalendarIntervals:
    def test_primary_plus_retries(self) -> None:
        cfg = _cfg(primary_time="06:00", retry_times=("09:00", "19:00", "23:00"))
        intervals = _build_calendar_intervals(cfg)
        assert len(intervals) == 4
        assert {"Hour": 6, "Minute": 0} in intervals
        assert {"Hour": 9, "Minute": 0} in intervals
        assert {"Hour": 19, "Minute": 0} in intervals
        assert {"Hour": 23, "Minute": 0} in intervals

    def test_no_retry_times(self) -> None:
        cfg = _cfg(primary_time="07:30", retry_times=())
        intervals = _build_calendar_intervals(cfg)
        assert intervals == [{"Hour": 7, "Minute": 30}]

    def test_deduplicates_times(self) -> None:
        cfg = _cfg(primary_time="06:00", retry_times=("06:00", "09:00"))
        intervals = _build_calendar_intervals(cfg)
        hours = [i["Hour"] for i in intervals]
        assert hours.count(6) == 1  # deduplicated

    def test_empty_retry_strings_skipped(self) -> None:
        cfg = _cfg(primary_time="06:00", retry_times=("", "09:00"))
        intervals = _build_calendar_intervals(cfg)
        assert len(intervals) == 2


# ---------------------------------------------------------------------------
# build_plist
# ---------------------------------------------------------------------------


class TestBuildPlist:
    def test_plist_keys_present(self, tmp_path: Path) -> None:
        cfg = _cfg()
        repo = tmp_path / "repo"
        py = tmp_path / "python"
        log = tmp_path / "logs"
        plist = build_plist(cfg, repo, py, log)

        assert plist["Label"] == LABEL
        assert "ProgramArguments" in plist
        assert "betfair_results_downloader" in " ".join(
            str(a) for a in plist["ProgramArguments"]
        )
        assert "run" in plist["ProgramArguments"]
        assert str(repo) == plist["WorkingDirectory"]
        assert "StartCalendarInterval" in plist
        assert "StandardOutPath" in plist
        assert "StandardErrorPath" in plist

    def test_produces_valid_xml_plist(self, tmp_path: Path) -> None:
        cfg = _cfg()
        repo = tmp_path / "repo"
        py = tmp_path / "python3"
        log = tmp_path / "logs"
        plist_dict = build_plist(cfg, repo, py, log)

        # Must be serialisable to valid XML
        xml_bytes = plistlib.dumps(plist_dict, fmt=plistlib.FMT_XML)
        assert b"<plist" in xml_bytes

        # Round-trip: parse back and verify key fields
        recovered = plistlib.loads(xml_bytes)
        assert recovered["Label"] == LABEL
        assert isinstance(recovered["StartCalendarInterval"], list)
        assert len(recovered["StartCalendarInterval"]) == 4  # primary + 3 retries

    def test_calendar_interval_count_matches_times(self, tmp_path: Path) -> None:
        cfg = _cfg(primary_time="07:00", retry_times=("10:00",))
        plist = build_plist(cfg, tmp_path, tmp_path / "py", tmp_path / "logs")
        assert len(plist["StartCalendarInterval"]) == 2

    def test_log_dir_created(self, tmp_path: Path) -> None:
        log = tmp_path / "a" / "b" / "logs"
        build_plist(_cfg(), tmp_path, tmp_path / "py", log)
        assert log.exists()


# ---------------------------------------------------------------------------
# LaunchdInstaller — install / uninstall (dry-run, no launchctl)
# ---------------------------------------------------------------------------


class TestLaunchdInstallerDryRun:
    def test_install_dry_run_writes_nothing(self, tmp_path: Path) -> None:
        """A dry run must not change the installed schedule."""
        cfg = _cfg()
        installer = LaunchdInstaller()

        # Redirect the plist to a temp location so we don't write to ~/Library
        plist_target = tmp_path / "com.betfair.results.scheduler.plist"
        agents_target = tmp_path

        with (
            patch(
                "betfair_results_downloader.scheduler.installers.launchd.PLIST_PATH",
                plist_target,
            ),
            patch(
                "betfair_results_downloader.scheduler.installers.launchd.AGENTS_DIR",
                agents_target,
            ),
        ):
            result = installer.install(
                schedule_cfg=cfg,
                repo_root=tmp_path,
                venv_python_path=Path(sys.executable),
                log_dir=tmp_path / "logs",
                dry_run=True,
            )

        assert result["ok"] is True
        assert not plist_target.exists(), (
            "dry run must not write the plist -- it previously wrote it before "
            "checking the flag, so the one run meant to change nothing did"
        )
        assert "Dry run" in result["message"]

    def test_uninstall_dry_run_removes_nothing(self, tmp_path: Path) -> None:
        plist_target = tmp_path / "com.betfair.results.scheduler.plist"
        plist_target.write_bytes(b"placeholder")

        installer = LaunchdInstaller()
        with patch(
            "betfair_results_downloader.scheduler.installers.launchd.PLIST_PATH",
            plist_target,
        ):
            result = installer.uninstall(dry_run=True)

        assert result["ok"] is True
        assert plist_target.exists(), "dry run must not remove the plist"
        assert "Dry run" in result["message"]

    def test_uninstall_when_not_installed(self) -> None:
        installer = LaunchdInstaller()
        with patch(
            "betfair_results_downloader.scheduler.installers.launchd.PLIST_PATH",
            Path("/nonexistent/nowhere.plist"),
        ):
            result = installer.uninstall(dry_run=True)
        assert result["ok"] is True
        assert "nothing to uninstall" in result["message"].lower()

    def test_plist_content_has_correct_times(self, tmp_path: Path) -> None:
        """
        Build the plist directly rather than through install(dry_run=True):
        a dry run no longer writes anything, and using it as a way to produce
        a file to inspect was relying on the bug it is named after.
        """
        cfg = _cfg(primary_time="07:30", retry_times=("11:00",))

        data = build_plist(cfg, tmp_path, Path(sys.executable), tmp_path / "logs")

        intervals = data["StartCalendarInterval"]
        assert {"Hour": 7, "Minute": 30} in intervals
        assert {"Hour": 11, "Minute": 0} in intervals
        assert len(intervals) == 2, "exactly the configured times, no more"
        assert data["Label"] == LABEL


class TestInstalledTimesReporting:
    def test_installed_times_read_from_the_plist(self, tmp_path: Path) -> None:
        """
        `schedule install --time` overrides the plist for that install only
        and writes nothing back to credentials.json, so the configured and
        installed schedules can diverge with nothing to reveal it. status()
        now reports what is actually installed.
        """
        from betfair_results_downloader.scheduler.installers import launchd

        plist_target = tmp_path / f"{LABEL}.plist"
        data = build_plist(
            _cfg(primary_time="07:30", retry_times=("11:00", "23:15")),
            tmp_path,
            Path(sys.executable),
            tmp_path / "logs",
        )
        plist_target.write_bytes(plistlib.dumps(data))

        with patch.object(launchd, "PLIST_PATH", plist_target):
            assert launchd._installed_times() == ["07:30", "11:00", "23:15"]

    def test_installed_times_empty_when_absent(self, tmp_path: Path) -> None:
        from betfair_results_downloader.scheduler.installers import launchd

        with patch.object(launchd, "PLIST_PATH", tmp_path / "nope.plist"):
            assert launchd._installed_times() == []

    def test_installed_times_survives_a_corrupt_plist(self, tmp_path: Path) -> None:
        from betfair_results_downloader.scheduler.installers import launchd

        bad = tmp_path / f"{LABEL}.plist"
        bad.write_bytes(b"not a plist")
        with patch.object(launchd, "PLIST_PATH", bad):
            assert launchd._installed_times() == []


def test_status_does_not_crash_on_a_dash_pid(monkeypatch, tmp_path: Path) -> None:
    """
    launchctl prints "-" for a job that has not run yet -- the state an
    unloaded-then-reloaded agent is in. int("-") took `schedule status` down.
    """
    from betfair_results_downloader.scheduler.installers import launchd

    plist_target = tmp_path / f"{LABEL}.plist"
    plist_target.write_bytes(plistlib.dumps({"Label": LABEL}))

    class _Result:
        stdout = f"-\t-\t{LABEL}\n"

    monkeypatch.setattr(launchd.subprocess, "run", lambda *a, **k: _Result())

    with patch.object(launchd, "PLIST_PATH", plist_target):
        info = launchd.LaunchdInstaller().status()

    assert info["loaded"] is True
    assert info["pid"] is None
    assert info["last_exit"] is None

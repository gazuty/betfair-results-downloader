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
        min_coverage_overlap_days=1,
        log_dir="",
        history_file="",
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
        assert "betfair_results_downloader" in " ".join(str(a) for a in plist["ProgramArguments"])
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
    def test_install_dry_run_writes_plist(self, tmp_path: Path) -> None:
        cfg = _cfg()
        installer = LaunchdInstaller()

        # Redirect the plist to a temp location so we don't write to ~/Library
        plist_target = tmp_path / "com.betfair.results.scheduler.plist"
        agents_target = tmp_path

        with patch("betfair_results_downloader.scheduler.installers.launchd.PLIST_PATH",
                   plist_target), \
             patch("betfair_results_downloader.scheduler.installers.launchd.AGENTS_DIR",
                   agents_target):
            result = installer.install(
                schedule_cfg=cfg,
                repo_root=tmp_path,
                venv_python_path=Path(sys.executable),
                log_dir=tmp_path / "logs",
                dry_run=True,
            )

        assert result["ok"] is True
        assert plist_target.exists()
        # Verify it's valid plist XML
        data = plistlib.loads(plist_target.read_bytes())
        assert data["Label"] == LABEL

    def test_uninstall_dry_run_removes_plist(self, tmp_path: Path) -> None:
        plist_target = tmp_path / "com.betfair.results.scheduler.plist"
        plist_target.write_bytes(b"placeholder")

        installer = LaunchdInstaller()
        with patch("betfair_results_downloader.scheduler.installers.launchd.PLIST_PATH",
                   plist_target):
            result = installer.uninstall(dry_run=True)

        assert result["ok"] is True
        assert not plist_target.exists()

    def test_uninstall_when_not_installed(self) -> None:
        installer = LaunchdInstaller()
        with patch("betfair_results_downloader.scheduler.installers.launchd.PLIST_PATH",
                   Path("/nonexistent/nowhere.plist")):
            result = installer.uninstall(dry_run=True)
        assert result["ok"] is True
        assert "nothing to uninstall" in result["message"].lower()

    def test_plist_content_has_correct_times(self, tmp_path: Path) -> None:
        cfg = _cfg(primary_time="07:30", retry_times=("11:00",))
        plist_target = tmp_path / f"{LABEL}.plist"

        installer = LaunchdInstaller()
        with patch("betfair_results_downloader.scheduler.installers.launchd.PLIST_PATH",
                   plist_target), \
             patch("betfair_results_downloader.scheduler.installers.launchd.AGENTS_DIR",
                   tmp_path):
            installer.install(
                schedule_cfg=cfg,
                repo_root=tmp_path,
                venv_python_path=Path(sys.executable),
                log_dir=tmp_path / "logs",
                dry_run=True,
            )

        data = plistlib.loads(plist_target.read_bytes())
        intervals = data["StartCalendarInterval"]
        assert {"Hour": 7, "Minute": 30} in intervals
        assert {"Hour": 11, "Minute": 0} in intervals
        assert len(intervals) == 2

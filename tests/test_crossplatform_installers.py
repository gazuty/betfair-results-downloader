"""Tests for Phase 3.2: Windows Task Scheduler, Linux systemd, and cron installers."""
from __future__ import annotations

import sys
from pathlib import Path


from betfair_results_downloader.config import ScheduleConfig
from betfair_results_downloader.scheduler.installers.taskscheduler import (
    TASK_NAME,
    TaskSchedulerInstaller,
    _pythonw_from,
    build_task_xml,
)
from betfair_results_downloader.scheduler.installers.systemd_user import (
    SERVICE_NAME,
    SystemdUserInstaller,
    build_service_unit,
    build_timer_unit,
)
from betfair_results_downloader.scheduler.installers.cron import (
    MARKER_COMMENT,
    CronInstaller,
    build_cron_line,
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
        history_file="",
    )
    defaults.update(overrides)
    return ScheduleConfig(**defaults)


PY = Path(sys.executable)


# ===========================================================================
# Windows Task Scheduler
# ===========================================================================

class TestBuildTaskXml:
    def test_produces_valid_xml(self, tmp_path: Path) -> None:
        xml_str = build_task_xml(_cfg(), tmp_path, PY)
        # Should contain key Task Scheduler XML structural elements
        assert "<Task" in xml_str
        assert "Triggers" in xml_str
        assert "Actions" in xml_str
        assert "Settings" in xml_str
        assert "CalendarTrigger" in xml_str

    def test_xml_contains_correct_number_of_triggers(self, tmp_path: Path) -> None:
        cfg = _cfg(primary_time="06:00", retry_times=("09:00", "19:00"))
        xml_str = build_task_xml(cfg, tmp_path, PY)
        count = xml_str.count("CalendarTrigger")
        # Each trigger appears as open + close tags
        assert count >= 2  # 3 time entries = 3 CalendarTrigger pairs

    def test_xml_contains_python_command(self, tmp_path: Path) -> None:
        xml_str = build_task_xml(_cfg(), tmp_path, PY)
        assert "betfair_results_downloader" in xml_str
        assert "run" in xml_str

    def test_xml_working_directory_set(self, tmp_path: Path) -> None:
        xml_str = build_task_xml(_cfg(), tmp_path, PY)
        assert str(tmp_path) in xml_str

    def test_deduplicates_times(self, tmp_path: Path) -> None:
        cfg = _cfg(primary_time="06:00", retry_times=("06:00", "09:00"))
        xml_str = build_task_xml(cfg, tmp_path, PY)
        # 2 unique times: 06:00 and 09:00
        count = xml_str.count("T06:00:00") + xml_str.count("T09:00:00")
        assert count >= 2


class TestPythonwFrom:
    def test_returns_original_when_no_pythonw(self, tmp_path: Path) -> None:
        fake_py = tmp_path / "python.exe"
        fake_py.touch()
        result = _pythonw_from(fake_py)
        # pythonw.exe doesn't exist → return original
        assert result == fake_py

    def test_returns_pythonw_when_exists(self, tmp_path: Path) -> None:
        fake_py = tmp_path / "python.exe"
        fake_py.touch()
        fake_pyw = tmp_path / "pythonw.exe"
        fake_pyw.touch()
        result = _pythonw_from(fake_py)
        assert result == fake_pyw


class TestTaskSchedulerInstallerDryRun:
    def test_install_dry_run_writes_xml(self, tmp_path: Path) -> None:
        installer = TaskSchedulerInstaller()
        result = installer.install(
            schedule_cfg=_cfg(),
            repo_root=tmp_path,
            venv_python_path=PY,
            log_dir=tmp_path / "logs",
            dry_run=True,
        )
        assert result["ok"] is True
        xml_path = Path(result["xml_path"])
        assert xml_path.exists()
        content = xml_path.read_text(encoding="utf-16")
        assert "betfair_results_downloader" in content

    def test_uninstall_dry_run(self) -> None:
        installer = TaskSchedulerInstaller()
        result = installer.uninstall(dry_run=True)
        assert result["ok"] is True
        assert TASK_NAME in result["message"]


# ===========================================================================
# Linux systemd --user
# ===========================================================================

class TestBuildServiceUnit:
    def test_contains_exec_start(self, tmp_path: Path) -> None:
        content = build_service_unit(tmp_path, PY)
        assert "ExecStart=" in content
        assert "betfair_results_downloader" in content
        assert "run" in content

    def test_contains_working_directory(self, tmp_path: Path) -> None:
        content = build_service_unit(tmp_path, PY)
        assert str(tmp_path) in content

    def test_service_type_is_oneshot(self, tmp_path: Path) -> None:
        content = build_service_unit(tmp_path, PY)
        assert "Type=oneshot" in content


class TestBuildTimerUnit:
    def test_has_on_calendar_for_each_time(self) -> None:
        cfg = _cfg(primary_time="06:00", retry_times=("09:00", "19:00"))
        content = build_timer_unit(cfg)
        assert "OnCalendar=*-*-* 06:00:00" in content
        assert "OnCalendar=*-*-* 09:00:00" in content
        assert "OnCalendar=*-*-* 19:00:00" in content

    def test_persistent_is_true(self) -> None:
        content = build_timer_unit(_cfg())
        assert "Persistent=true" in content

    def test_unit_references_service(self) -> None:
        content = build_timer_unit(_cfg())
        assert f"Unit={SERVICE_NAME}.service" in content

    def test_deduplicates_times(self) -> None:
        cfg = _cfg(primary_time="06:00", retry_times=("06:00", "09:00"))
        content = build_timer_unit(cfg)
        # 06:00 should appear exactly once
        assert content.count("OnCalendar=*-*-* 06:00:00") == 1


class TestSystemdInstallerDryRun:
    def test_install_dry_run_writes_files(self, tmp_path: Path) -> None:
        from unittest.mock import patch
        svc = tmp_path / f"{SERVICE_NAME}.service"
        tmr = tmp_path / f"{SERVICE_NAME}.timer"

        installer = SystemdUserInstaller()
        with patch("betfair_results_downloader.scheduler.installers.systemd_user.SYSTEMD_USER_DIR",
                   tmp_path), \
             patch("betfair_results_downloader.scheduler.installers.systemd_user.SERVICE_FILE",
                   svc), \
             patch("betfair_results_downloader.scheduler.installers.systemd_user.TIMER_FILE",
                   tmr):
            result = installer.install(
                schedule_cfg=_cfg(),
                repo_root=tmp_path,
                venv_python_path=PY,
                dry_run=True,
            )

        assert result["ok"] is True
        assert svc.exists()
        assert tmr.exists()
        assert "Persistent=true" in tmr.read_text()

    def test_uninstall_dry_run_removes_files(self, tmp_path: Path) -> None:
        from unittest.mock import patch
        svc = tmp_path / f"{SERVICE_NAME}.service"
        tmr = tmp_path / f"{SERVICE_NAME}.timer"
        svc.write_text("placeholder")
        tmr.write_text("placeholder")

        installer = SystemdUserInstaller()
        with patch("betfair_results_downloader.scheduler.installers.systemd_user.SERVICE_FILE", svc), \
             patch("betfair_results_downloader.scheduler.installers.systemd_user.TIMER_FILE", tmr):
            result = installer.uninstall(dry_run=True)

        assert result["ok"] is True
        assert not svc.exists()
        assert not tmr.exists()


# ===========================================================================
# Cron
# ===========================================================================

class TestBuildCronLine:
    def test_contains_marker_comment(self, tmp_path: Path) -> None:
        line = build_cron_line(_cfg(), tmp_path, PY, tmp_path / "logs")
        assert MARKER_COMMENT in line

    def test_hours_derived_from_times(self, tmp_path: Path) -> None:
        cfg = _cfg(primary_time="06:00", retry_times=("09:00", "19:00", "23:00"))
        line = build_cron_line(cfg, tmp_path, PY, tmp_path / "logs")
        assert "6,9,19,23" in line or all(str(h) in line for h in [6, 9, 19, 23])

    def test_python_path_in_line(self, tmp_path: Path) -> None:
        line = build_cron_line(_cfg(), tmp_path, PY, tmp_path / "logs")
        assert str(PY) in line

    def test_contains_run_command(self, tmp_path: Path) -> None:
        line = build_cron_line(_cfg(), tmp_path, PY, tmp_path / "logs")
        assert "betfair_results_downloader" in line
        assert "run" in line

    def test_redirects_to_log_file(self, tmp_path: Path) -> None:
        log_dir = tmp_path / "logs"
        line = build_cron_line(_cfg(), tmp_path, PY, log_dir)
        assert "cron.log" in line


class TestBuildCronLineMinutes:
    def test_distinct_minutes_get_their_own_lines(self, tmp_path: Path) -> None:
        cfg = _cfg(primary_time="06:30", retry_times=("09:00", "23:00"))
        block = build_cron_line(cfg, tmp_path, PY, tmp_path / "logs")
        lines = block.splitlines()
        assert any(ln.startswith("30 6 ") for ln in lines)
        assert any(ln.startswith("0 9,23 ") for ln in lines)

    def test_every_managed_line_carries_the_marker(self, tmp_path: Path) -> None:
        cfg = _cfg(primary_time="06:30", retry_times=("09:00",))
        block = build_cron_line(cfg, tmp_path, PY, tmp_path / "logs")
        for ln in block.splitlines():
            assert MARKER_COMMENT in ln

    def test_strip_removes_current_and_legacy_formats(self, tmp_path: Path) -> None:
        from betfair_results_downloader.scheduler.installers.cron import _strip_managed_lines
        cfg = _cfg(primary_time="06:30", retry_times=("09:00",))
        block = build_cron_line(cfg, tmp_path, PY, tmp_path / "logs")
        legacy = [MARKER_COMMENT, "0 6 * * * old-command"]
        user_line = "15 4 * * * some-user-job"
        crontab = [user_line, *legacy, *block.splitlines()]
        assert _strip_managed_lines(crontab) == [user_line]


class TestCronInstallerDryRun:
    def test_install_dry_run_returns_entry(self, tmp_path: Path) -> None:
        installer = CronInstaller()
        result = installer.install(
            schedule_cfg=_cfg(),
            repo_root=tmp_path,
            venv_python_path=PY,
            log_dir=tmp_path / "logs",
            dry_run=True,
        )
        assert result["ok"] is True
        assert MARKER_COMMENT in result["entry"]
        assert "betfair_results_downloader" in result["entry"]

    def test_uninstall_dry_run(self) -> None:
        installer = CronInstaller()
        result = installer.uninstall(dry_run=True)
        assert result["ok"] is True
        assert "dry-run" in result["message"].lower()

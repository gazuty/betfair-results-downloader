"""
scheduler/installers/systemd_user.py
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Linux systemd --user installer (Phase 3.2).

Generates a ``.service`` and ``.timer`` unit file for the current user,
writes them to ``~/.config/systemd/user/``, then enables+starts them via
``systemctl --user enable --now``.

The timer uses ``OnCalendar`` entries (one per scheduled time) and
``Persistent=true`` so missed runs are caught on next boot/login.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any

from ...config import ScheduleConfig


SERVICE_NAME = "betfair-results"
SYSTEMD_USER_DIR = Path.home() / ".config" / "systemd" / "user"
SERVICE_FILE = SYSTEMD_USER_DIR / f"{SERVICE_NAME}.service"
TIMER_FILE = SYSTEMD_USER_DIR / f"{SERVICE_NAME}.timer"


def build_service_unit(
    repo_root: Path,
    python_path: Path,
    log_dir: Path | None = None,
) -> str:
    """
    Build the systemd .service unit file content.

    Parameters
    ----------
    repo_root:
        Repository root (WorkingDirectory).
    python_path:
        Python interpreter path.
    log_dir:
        Unused (systemd directs output to journald by default).

    Returns
    -------
    str
        systemd unit file as a string.
    """
    return f"""\
[Unit]
Description=Betfair Results daily download
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
ExecStart={python_path} -m betfair_results_downloader run
WorkingDirectory={repo_root}
StandardOutput=journal
StandardError=journal
SyslogIdentifier={SERVICE_NAME}

[Install]
WantedBy=default.target
"""


def build_timer_unit(schedule_cfg: ScheduleConfig) -> str:
    """
    Build the systemd .timer unit file content.

    Each scheduled time becomes an ``OnCalendar=*-*-* HH:MM:SS`` line.
    ``Persistent=true`` ensures missed jobs are retried on boot.

    Parameters
    ----------
    schedule_cfg:
        Schedule configuration (source of times).

    Returns
    -------
    str
        systemd timer unit file as a string.
    """
    all_times = [schedule_cfg.primary_time] + list(schedule_cfg.retry_times)
    seen: set[str] = set()
    calendar_lines = []
    for t in all_times:
        t = t.strip()
        if not t or t in seen:
            continue
        seen.add(t)
        calendar_lines.append(f"OnCalendar=*-*-* {t}:00")

    calendars = "\n".join(calendar_lines)
    return f"""\
[Unit]
Description=Betfair Results scheduler timer

[Timer]
{calendars}
Persistent=true
Unit={SERVICE_NAME}.service

[Install]
WantedBy=timers.target
"""


class SystemdUserInstaller:
    """
    Linux systemd --user scheduler installer.

    Writes unit files and manages them via ``systemctl --user``.
    """

    def install(
        self,
        schedule_cfg: ScheduleConfig,
        repo_root: Path,
        venv_python_path: Path | None = None,
        log_dir: Path | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        py = Path(venv_python_path or sys.executable)
        service_content = build_service_unit(repo_root, py, log_dir)
        timer_content = build_timer_unit(schedule_cfg)

        SYSTEMD_USER_DIR.mkdir(parents=True, exist_ok=True)
        SERVICE_FILE.write_text(service_content, encoding="utf-8")
        TIMER_FILE.write_text(timer_content, encoding="utf-8")

        if dry_run:
            return {
                "ok": True,
                "service_file": str(SERVICE_FILE),
                "timer_file": str(TIMER_FILE),
                "message": "Unit files written (dry-run, systemctl not called).",
            }

        # Reload daemon so systemd sees new unit files
        subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)

        result = subprocess.run(
            ["systemctl", "--user", "enable", "--now", f"{SERVICE_NAME}.timer"],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            return {
                "ok": False,
                "message": (
                    f"Unit files written but systemctl enable failed "
                    f"(exit {result.returncode}): {result.stderr.strip()}"
                ),
            }
        return {
            "ok": True,
            "message": (
                f"Installed and enabled {SERVICE_NAME}.timer. "
                f"Service: {SERVICE_FILE}  Timer: {TIMER_FILE}"
            ),
        }

    def uninstall(self, dry_run: bool = False) -> dict[str, Any]:
        if not dry_run:
            subprocess.run(
                ["systemctl", "--user", "disable", "--now", f"{SERVICE_NAME}.timer"],
                capture_output=True,
            )
        SERVICE_FILE.unlink(missing_ok=True)
        TIMER_FILE.unlink(missing_ok=True)
        if not dry_run:
            subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)
        return {"ok": True, "message": f"Uninstalled {SERVICE_NAME} timer and service."}

    def status(self) -> dict[str, Any]:
        result = subprocess.run(
            ["systemctl", "--user", "is-active", f"{SERVICE_NAME}.timer"],
            capture_output=True, text=True,
        )
        active = result.stdout.strip() == "active"
        return {
            "installed": SERVICE_FILE.exists(),
            "loaded": active,
            "pid": None,
            "last_exit": None,
            "message": (
                f"{SERVICE_NAME}.timer: {'active' if active else 'inactive/not-loaded'}. "
                f"Unit files: service={'present' if SERVICE_FILE.exists() else 'missing'}, "
                f"timer={'present' if TIMER_FILE.exists() else 'missing'}."
            ),
        }

    def logs(self, log_dir: Path, tail_n: int = 50) -> str:
        import json
        output_parts: list[str] = []

        # run_history.jsonl
        jsonl_path = log_dir / "run_history.jsonl"
        if jsonl_path.exists():
            lines = jsonl_path.read_text(encoding="utf-8").splitlines()
            recent = lines[-tail_n:]
            output_parts.append(f"=== run_history.jsonl (last {len(recent)}) ===")
            for line in recent:
                try:
                    data = json.loads(line)
                    output_parts.append(
                        f"  {data.get('ts', '?')[:19]}  status={data.get('status')}  "
                        f"{data.get('message', '')[:80]}"
                    )
                except Exception:
                    output_parts.append(f"  {line[:120]}")

        # Journald
        output_parts.append(f"\n(Tip: journalctl --user -u {SERVICE_NAME} -n {tail_n} for full logs)")
        return "\n".join(output_parts) if output_parts else "No logs found."

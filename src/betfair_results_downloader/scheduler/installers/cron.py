"""
scheduler/installers/cron.py
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Unix cron fallback installer (Phase 3.2).

Used on Linux systems without systemd (or when systemd is not available).
Installs a crontab entry with an idempotent marker comment so repeat installs
don't add duplicate lines.

The generated crontab line runs ``betfair-results run`` at each scheduled
time (primary + retries).  Hours are derived from the HH:MM time strings.

Example for primary=06:00, retries=09:00,19:00,23:00::

    # BETFAIR_RESULTS_SCHEDULER
    0 6,9,19,23 * * * cd /path/to/repo && /path/to/python -m betfair_results_downloader run >> /path/to/outputs/cron.log 2>&1
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any

from ...config import ScheduleConfig


MARKER_COMMENT = "# BETFAIR_RESULTS_SCHEDULER"


def build_cron_line(
    schedule_cfg: ScheduleConfig,
    repo_root: Path,
    python_path: Path,
    log_dir: Path,
) -> str:
    """
    Build the crontab entry for the given schedule config.

    One line per hour derived from all scheduled times (primary + retries).
    Minutes from the primary_time are used; retries use the same minute.

    Parameters
    ----------
    schedule_cfg:
        Schedule configuration.
    repo_root:
        Repository root (cd target).
    python_path:
        Python interpreter path.
    log_dir:
        Directory for cron.log output.

    Returns
    -------
    str
        Crontab entry (two lines: marker comment + cron expression).
    """
    all_times = [schedule_cfg.primary_time] + list(schedule_cfg.retry_times)
    hours: list[int] = []
    minute = 0
    seen: set[int] = set()
    for i, t in enumerate(all_times):
        t = t.strip()
        if not t:
            continue
        parts = t.split(":")
        h = int(parts[0])
        if i == 0:
            minute = int(parts[1]) if len(parts) > 1 else 0
        if h not in seen:
            seen.add(h)
            hours.append(h)

    hours_str = ",".join(str(h) for h in hours)
    log_file = log_dir / "cron.log"
    cron_expr = (
        f"{minute} {hours_str} * * * "
        f"cd {repo_root} && {python_path} -m betfair_results_downloader run "
        f">> {log_file} 2>&1"
    )
    return f"{MARKER_COMMENT}\n{cron_expr}"


class CronInstaller:
    """
    Unix cron fallback installer.

    Adds/removes a marked crontab entry.  Idempotent: re-installing replaces
    the existing entry rather than adding a duplicate.
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
        log = log_dir or (repo_root / "outputs")
        log.mkdir(parents=True, exist_ok=True)

        new_entry = build_cron_line(schedule_cfg, repo_root, py, log)

        if dry_run:
            return {
                "ok": True,
                "entry": new_entry,
                "message": f"Crontab entry (dry-run, not installed):\n{new_entry}",
            }

        # Read existing crontab (ignore error if none exists yet)
        existing_result = subprocess.run(
            ["crontab", "-l"], capture_output=True, text=True
        )
        existing = existing_result.stdout if existing_result.returncode == 0 else ""

        # Remove any previous managed entry (marker + next line)
        lines = existing.splitlines()
        cleaned: list[str] = []
        skip_next = False
        for line in lines:
            if skip_next:
                skip_next = False
                continue
            if line.strip() == MARKER_COMMENT:
                skip_next = True
                continue
            cleaned.append(line)

        # Append new entry
        cleaned_str = "\n".join(cleaned).rstrip("\n")
        new_crontab = (cleaned_str + "\n" + new_entry + "\n").lstrip("\n")

        write_result = subprocess.run(
            ["crontab", "-"],
            input=new_crontab,
            capture_output=True,
            text=True,
        )
        if write_result.returncode != 0:
            return {
                "ok": False,
                "message": f"crontab write failed: {write_result.stderr.strip()}",
            }
        return {
            "ok": True,
            "entry": new_entry,
            "message": f"Crontab entry installed:\n{new_entry}",
        }

    def uninstall(self, dry_run: bool = False) -> dict[str, Any]:
        if dry_run:
            return {"ok": True, "message": "Would remove crontab entry (dry-run)."}

        existing_result = subprocess.run(
            ["crontab", "-l"], capture_output=True, text=True
        )
        if existing_result.returncode != 0:
            return {"ok": True, "message": "No crontab found — nothing to remove."}

        lines = existing_result.stdout.splitlines()
        cleaned: list[str] = []
        skip_next = False
        for line in lines:
            if skip_next:
                skip_next = False
                continue
            if line.strip() == MARKER_COMMENT:
                skip_next = True
                continue
            cleaned.append(line)

        new_crontab = "\n".join(cleaned).strip() + "\n"
        subprocess.run(["crontab", "-"], input=new_crontab, capture_output=True, text=True)
        return {"ok": True, "message": "Betfair Results crontab entry removed."}

    def status(self) -> dict[str, Any]:
        result = subprocess.run(
            ["crontab", "-l"], capture_output=True, text=True
        )
        if result.returncode != 0:
            return {
                "installed": False, "loaded": False, "pid": None, "last_exit": None,
                "message": "No crontab found.",
            }
        installed = MARKER_COMMENT in result.stdout
        return {
            "installed": installed,
            "loaded": installed,
            "pid": None,
            "last_exit": None,
            "message": (
                f"Betfair Results crontab entry: {'found' if installed else 'not found'}."
            ),
        }

    def logs(self, log_dir: Path, tail_n: int = 50) -> str:
        import json
        output_parts: list[str] = []

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

        cron_log = log_dir / "cron.log"
        if cron_log.exists():
            lines = cron_log.read_text(encoding="utf-8", errors="replace").splitlines()
            recent = lines[-tail_n:]
            output_parts.append(f"\n=== cron.log (last {len(recent)} lines) ===")
            output_parts.extend(f"  {ln}" for ln in recent)

        return "\n".join(output_parts) if output_parts else "No logs found."

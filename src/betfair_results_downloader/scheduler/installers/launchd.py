"""
scheduler/installers/launchd.py
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
macOS launchd-based scheduler installer (Phase 3.1).

Generates a LaunchAgent plist with ``StartCalendarInterval`` entries derived
from ``schedule.primary_time`` + ``schedule.retry_times``, then loads/unloads
it via ``launchctl bootstrap``/``bootout``.

Plist label: ``com.betfair.results.scheduler``
Plist path:  ``~/Library/LaunchAgents/com.betfair.results.scheduler.plist``
"""

from __future__ import annotations

import os
import plistlib
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from ...config import ScheduleConfig


LABEL = "com.betfair.results.scheduler"
AGENTS_DIR = Path.home() / "Library" / "LaunchAgents"
PLIST_PATH = AGENTS_DIR / f"{LABEL}.plist"

# The hourly racecard capture (see racecard). Its own job, not extra times on
# the scheduler: a capture is one login and a few catalogue calls, while a
# scheduled run downloads, rewrites the canonical and publishes to Azure.
RACECARD_LABEL = "com.betfair.results.racecard"
RACECARD_PLIST_PATH = AGENTS_DIR / f"{RACECARD_LABEL}.plist"

# Half past every hour, clear of the scheduled runs on the hour. A
# StartCalendarInterval with only a Minute key fires every hour; racing runs
# around the clock in Sydney time (UK and US meetings run overnight), so
# there are no quiet hours to skip.
RACECARD_MINUTE = 30

# The hourly retry (see scheduler.retry): its own job so the scheduler's four
# times stay what `schedule status` reports, and so it can be removed without
# touching them. On the hour, clear of the racecard capture at half past.
RETRY_LABEL = "com.betfair.results.retry"
RETRY_PLIST_PATH = AGENTS_DIR / f"{RETRY_LABEL}.plist"
RETRY_MINUTE = 0


def _parse_hh_mm(time_str: str) -> tuple[int, int]:
    """Parse 'HH:MM' into (hour, minute). Raises ValueError on bad input."""
    parts = time_str.strip().split(":")
    if len(parts) != 2:
        raise ValueError(f"Expected HH:MM, got: {time_str!r}")
    return int(parts[0]), int(parts[1])


def _build_calendar_intervals(schedule_cfg: ScheduleConfig) -> list[dict[str, int]]:
    """
    Build a list of ``StartCalendarInterval`` dicts from the schedule config.

    Each entry triggers once per day at the specified hour:minute.
    """
    all_times = [schedule_cfg.primary_time] + list(schedule_cfg.retry_times)
    intervals = []
    seen: set[str] = set()
    for t in all_times:
        if not t:
            continue
        key = t.strip()
        if key in seen:
            continue
        seen.add(key)
        h, m = _parse_hh_mm(key)
        intervals.append({"Hour": h, "Minute": m})
    return intervals


def build_plist(
    schedule_cfg: ScheduleConfig,
    repo_root: Path,
    venv_python_path: Path,
    log_dir: Path,
) -> dict[str, Any]:
    """
    Build the plist dict for the LaunchAgent.

    Parameters
    ----------
    schedule_cfg:
        Parsed schedule configuration.
    repo_root:
        Absolute path to the repository root (WorkingDirectory).
    venv_python_path:
        Absolute path to the Python interpreter to use.
    log_dir:
        Directory for stdout/stderr logs.

    Returns
    -------
    dict
        plist-compatible dict ready for ``plistlib.dumps``.
    """
    log_dir.mkdir(parents=True, exist_ok=True)
    intervals = _build_calendar_intervals(schedule_cfg)

    return {
        "Label": LABEL,
        "ProgramArguments": [
            str(venv_python_path),
            "-m",
            "betfair_results_downloader",
            "run",
        ],
        "WorkingDirectory": str(repo_root),
        "StartCalendarInterval": intervals,
        "StandardOutPath": str(log_dir / "launchd.out.log"),
        "StandardErrorPath": str(log_dir / "launchd.err.log"),
        "RunAtLoad": False,
        "KeepAlive": False,
    }


def build_racecard_plist(
    repo_root: Path,
    venv_python_path: Path,
    log_dir: Path,
) -> dict[str, Any]:
    """The plist dict for the hourly racecard job."""
    log_dir.mkdir(parents=True, exist_ok=True)
    return {
        "Label": RACECARD_LABEL,
        "ProgramArguments": [
            str(venv_python_path),
            "-m",
            "betfair_results_downloader",
            "racecard",
        ],
        "WorkingDirectory": str(repo_root),
        "StartCalendarInterval": [{"Minute": RACECARD_MINUTE}],
        "StandardOutPath": str(log_dir / "racecard.out.log"),
        "StandardErrorPath": str(log_dir / "racecard.err.log"),
        "RunAtLoad": False,
        "KeepAlive": False,
    }


def _scheduled_hours(schedule_cfg: ScheduleConfig) -> set[int]:
    return {entry["Hour"] for entry in _build_calendar_intervals(schedule_cfg)}


def build_retry_plist(
    schedule_cfg: ScheduleConfig,
    repo_root: Path,
    venv_python_path: Path,
    log_dir: Path,
) -> dict[str, Any]:
    """
    The plist dict for the hourly retry job: on the hour, every hour that
    has no scheduled run. In an hour that has one, that run is the attempt,
    and a retry beside it would only queue behind the pipeline lock.
    """
    log_dir.mkdir(parents=True, exist_ok=True)
    busy = _scheduled_hours(schedule_cfg)
    return {
        "Label": RETRY_LABEL,
        "ProgramArguments": [
            str(venv_python_path),
            "-m",
            "betfair_results_downloader",
            "retry",
        ],
        "WorkingDirectory": str(repo_root),
        "StartCalendarInterval": [
            {"Hour": hour, "Minute": RETRY_MINUTE}
            for hour in range(24)
            if hour not in busy
        ],
        "StandardOutPath": str(log_dir / "retry.out.log"),
        "StandardErrorPath": str(log_dir / "retry.err.log"),
        "RunAtLoad": False,
        "KeepAlive": False,
    }


def _get_gui_uid() -> str:
    """Return the current user's UID as a string for launchctl domain."""
    return str(os.getuid())


def _installed_times() -> list[str]:
    """
    The times actually baked into the installed plist.

    `schedule install --time` applies an override for that install only and
    writes nothing back to credentials.json, so the configured and installed
    schedules can diverge silently. Reporting both is what makes that
    visible.
    """
    if not PLIST_PATH.exists():
        return []
    try:
        data = plistlib.loads(PLIST_PATH.read_bytes())
    except Exception:
        return []
    intervals = data.get("StartCalendarInterval") or []
    if isinstance(intervals, dict):
        intervals = [intervals]
    return sorted(
        f"{int(i.get('Hour', 0)):02d}:{int(i.get('Minute', 0)):02d}"
        for i in intervals
        if isinstance(i, dict)
    )


# launchd can refuse a bootstrap that follows a bootout too closely, while
# it is still tearing the old job down ("Bootstrap failed: 5: Input/output
# error"). One retry after this pause covers it.
BOOTSTRAP_RETRY_SECONDS = 2.0


def _replace_loaded_job(label: str, plist_path: Path) -> str | None:
    """
    Load ``plist_path`` in place of any copy of ``label`` already loaded.

    Boots the loaded copy out first. Without that a reinstall with new
    times wrote the plist and then failed on "service already loaded":
    launchd kept the old times while ``schedule status``, which reads the
    file, reported the new ones. A bootout that fails because nothing was
    loaded is the normal first install. Booting out stops a run that is in
    progress, exactly as ``uninstall`` does.

    Returns ``None`` on success, else the message to show. The message
    says when the old job is gone and nothing replaced it: that job will
    never run, so it will never raise a failure alert of its own.
    """
    uid = _get_gui_uid()
    bootout = subprocess.run(
        ["launchctl", "bootout", f"gui/{uid}/{label}"],
        capture_output=True,
        text=True,
    )
    was_loaded = bootout.returncode == 0
    command = ["launchctl", "bootstrap", f"gui/{uid}", str(plist_path)]
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode != 0 and was_loaded:
        time.sleep(BOOTSTRAP_RETRY_SECONDS)
        result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode == 0:
        return None
    message = (
        f"Plist written to {plist_path} but launchctl bootstrap failed "
        f"(exit {result.returncode}): {result.stderr.strip()}"
    )
    if was_loaded:
        message += (
            f". The previous {label} job was unloaded first, so NO scheduled "
            f"job is loaded now and nothing will run until this install "
            f"succeeds: fix the cause and run it again."
        )
    return message


class LaunchdInstaller:
    """
    macOS launchd scheduler installer.

    All methods that call ``launchctl`` are designed to fail gracefully with
    clear error messages rather than raising exceptions.
    """

    def install(
        self,
        schedule_cfg: ScheduleConfig,
        repo_root: Path,
        venv_python_path: Path | None = None,
        log_dir: Path | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """
        Generate the plist and register it with launchd.

        Parameters
        ----------
        schedule_cfg:
            Schedule configuration (source of times).
        repo_root:
            Repository root directory.
        venv_python_path:
            Python interpreter path. Defaults to ``sys.executable``.
        log_dir:
            Log directory for launchd stdout/stderr. Defaults to
            ``repo_root/outputs``.
        dry_run:
            If True, generate the plist but do not call launchctl.

        Returns
        -------
        dict
            ``{"ok": bool, "plist_path": str, "message": str}``
        """
        py_path = Path(venv_python_path or sys.executable)
        log = log_dir or (repo_root / "outputs")
        plist_dict = build_plist(schedule_cfg, repo_root, py_path, log)

        plist_bytes = plistlib.dumps(plist_dict, fmt=plistlib.FMT_XML)

        if dry_run:
            # Report without touching anything. Previously the plist was
            # written before this check, so the one run that was supposed to
            # change nothing changed the installed schedule.
            return {
                "ok": True,
                "plist_path": str(PLIST_PATH),
                "message": f"Dry run: would write {PLIST_PATH} and load it via launchctl.",
            }

        AGENTS_DIR.mkdir(parents=True, exist_ok=True)
        PLIST_PATH.write_bytes(plist_bytes)

        failure = _replace_loaded_job(LABEL, PLIST_PATH)
        if failure:
            return {"ok": False, "plist_path": str(PLIST_PATH), "message": failure}

        return {
            "ok": True,
            "plist_path": str(PLIST_PATH),
            "message": (
                f"Installed and loaded: {LABEL}. "
                f"Plist at {PLIST_PATH}. "
                f"Times: {schedule_cfg.primary_time} + {list(schedule_cfg.retry_times)}."
            ),
        }

    def uninstall(self, dry_run: bool = False) -> dict[str, Any]:
        """
        Unload the LaunchAgent and remove the plist file.

        Returns
        -------
        dict
            ``{"ok": bool, "message": str}``
        """
        if not PLIST_PATH.exists():
            return {
                "ok": True,
                "message": f"Plist not found at {PLIST_PATH} — nothing to uninstall.",
            }

        if dry_run:
            # Report without touching anything. This previously deleted the
            # plist and only skipped the launchctl call, so a dry run
            # uninstalled the schedule.
            return {
                "ok": True,
                "message": f"Dry run: would unload {LABEL} and remove {PLIST_PATH}.",
            }

        uid = _get_gui_uid()
        # bootout may fail if not currently loaded — that's fine
        subprocess.run(
            ["launchctl", "bootout", f"gui/{uid}/{LABEL}"],
            capture_output=True,
            text=True,
        )

        PLIST_PATH.unlink(missing_ok=True)

        return {
            "ok": True,
            "message": f"Uninstalled {LABEL}. Plist removed from {PLIST_PATH}.",
        }

    def install_racecard(
        self,
        repo_root: Path,
        venv_python_path: Path | None = None,
        log_dir: Path | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """
        Write the racecard plist and (re)load it.

        Any loaded copy is booted out first, so re-running the install after
        a repo move or a Python change replaces the job instead of failing
        on "service already loaded".
        """
        py_path = Path(venv_python_path or sys.executable)
        log = log_dir or (repo_root / "outputs")
        if dry_run:
            return {
                "ok": True,
                "plist_path": str(RACECARD_PLIST_PATH),
                "message": (
                    f"Dry run: would write {RACECARD_PLIST_PATH} and load it via launchctl."
                ),
            }
        plist_bytes = plistlib.dumps(
            build_racecard_plist(repo_root, py_path, log), fmt=plistlib.FMT_XML
        )
        AGENTS_DIR.mkdir(parents=True, exist_ok=True)
        RACECARD_PLIST_PATH.write_bytes(plist_bytes)
        failure = _replace_loaded_job(RACECARD_LABEL, RACECARD_PLIST_PATH)
        if failure:
            return {
                "ok": False,
                "plist_path": str(RACECARD_PLIST_PATH),
                "message": failure,
            }
        return {
            "ok": True,
            "plist_path": str(RACECARD_PLIST_PATH),
            "message": (
                f"Installed and loaded: {RACECARD_LABEL} (hourly at "
                f":{RACECARD_MINUTE:02d}). Plist at {RACECARD_PLIST_PATH}."
            ),
        }

    def uninstall_racecard(self, dry_run: bool = False) -> dict[str, Any]:
        """Unload the racecard job and remove its plist."""
        if not RACECARD_PLIST_PATH.exists():
            return {
                "ok": True,
                "message": (
                    f"Plist not found at {RACECARD_PLIST_PATH} — nothing to uninstall."
                ),
            }
        if dry_run:
            return {
                "ok": True,
                "message": (
                    f"Dry run: would unload {RACECARD_LABEL} and remove "
                    f"{RACECARD_PLIST_PATH}."
                ),
            }
        subprocess.run(
            ["launchctl", "bootout", f"gui/{_get_gui_uid()}/{RACECARD_LABEL}"],
            capture_output=True,
            text=True,
        )
        RACECARD_PLIST_PATH.unlink(missing_ok=True)
        return {
            "ok": True,
            "message": f"Uninstalled {RACECARD_LABEL}. Plist removed.",
        }

    def install_retry(
        self,
        schedule_cfg: ScheduleConfig,
        repo_root: Path,
        venv_python_path: Path | None = None,
        log_dir: Path | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """
        Write the retry plist and (re)load it. Reinstall after changing the
        scheduled times: the retry hours are the ones those times leave free.
        """
        py_path = Path(venv_python_path or sys.executable)
        log = log_dir or (repo_root / "outputs")
        if dry_run:
            return {
                "ok": True,
                "plist_path": str(RETRY_PLIST_PATH),
                "message": (
                    f"Dry run: would write {RETRY_PLIST_PATH} and load it via launchctl."
                ),
            }
        plist = build_retry_plist(schedule_cfg, repo_root, py_path, log)
        AGENTS_DIR.mkdir(parents=True, exist_ok=True)
        RETRY_PLIST_PATH.write_bytes(plistlib.dumps(plist, fmt=plistlib.FMT_XML))
        failure = _replace_loaded_job(RETRY_LABEL, RETRY_PLIST_PATH)
        if failure:
            return {
                "ok": False,
                "plist_path": str(RETRY_PLIST_PATH),
                "message": failure,
            }
        hours = [entry["Hour"] for entry in plist["StartCalendarInterval"]]
        return {
            "ok": True,
            "plist_path": str(RETRY_PLIST_PATH),
            "message": (
                f"Installed and loaded: {RETRY_LABEL} (on the hour, {len(hours)} "
                f"hours a day; not at the scheduled hours "
                f"{sorted(_scheduled_hours(schedule_cfg))}). "
                f"Plist at {RETRY_PLIST_PATH}."
            ),
        }

    def uninstall_retry(self, dry_run: bool = False) -> dict[str, Any]:
        """Unload the retry job and remove its plist."""
        if not RETRY_PLIST_PATH.exists():
            return {
                "ok": True,
                "message": f"Plist not found at {RETRY_PLIST_PATH} — nothing to uninstall.",
            }
        if dry_run:
            return {
                "ok": True,
                "message": f"Dry run: would unload {RETRY_LABEL} and remove {RETRY_PLIST_PATH}.",
            }
        subprocess.run(
            ["launchctl", "bootout", f"gui/{_get_gui_uid()}/{RETRY_LABEL}"],
            capture_output=True,
            text=True,
        )
        RETRY_PLIST_PATH.unlink(missing_ok=True)
        return {"ok": True, "message": f"Uninstalled {RETRY_LABEL}. Plist removed."}

    def retry_status(self) -> dict[str, Any]:
        """Whether the retry job is installed and loaded."""
        installed = RETRY_PLIST_PATH.exists()
        loaded = False
        if installed:
            result = subprocess.run(
                ["launchctl", "list"], capture_output=True, text=True
            )
            loaded = any(
                line.split()[-1:] == [RETRY_LABEL]
                for line in result.stdout.splitlines()
            )
        return {"installed": installed, "loaded": loaded}

    def racecard_status(self) -> dict[str, Any]:
        """Whether the racecard job is installed and loaded."""
        installed = RACECARD_PLIST_PATH.exists()
        loaded = False
        if installed:
            result = subprocess.run(
                ["launchctl", "list"], capture_output=True, text=True
            )
            loaded = any(
                line.split()[-1:] == [RACECARD_LABEL]
                for line in result.stdout.splitlines()
            )
        return {"installed": installed, "loaded": loaded}

    def status(self) -> dict[str, Any]:
        """
        Return the current status of the LaunchAgent.

        Returns
        -------
        dict
            Keys: ``installed``, ``loaded``, ``pid``, ``last_exit``, ``message``.
        """
        installed = PLIST_PATH.exists()
        installed_times = _installed_times()
        if not installed:
            return {
                "installed": False,
                "loaded": False,
                "pid": None,
                "last_exit": None,
                "installed_times": [],
                "message": f"Not installed (plist not found at {PLIST_PATH}).",
            }

        result = subprocess.run(
            ["launchctl", "list"],
            capture_output=True,
            text=True,
        )
        lines = result.stdout.splitlines()
        for line in lines:
            if LABEL in line:
                # Format: PID   LastExit   Label
                parts = line.split()
                pid = parts[0] if parts[0] != "-" else None
                # launchctl prints "-" for a job that has not run yet, which
                # is exactly the state an unloaded-then-reloaded agent is in.
                # int("-") raised and took `schedule status` down with it.
                last_exit = None
                if len(parts) > 1:
                    try:
                        last_exit = int(parts[1])
                    except ValueError:
                        last_exit = None
                return {
                    "installed": True,
                    "loaded": True,
                    "pid": pid,
                    "last_exit": last_exit,
                    "installed_times": installed_times,
                    "message": f"{LABEL} is loaded (PID={pid}, last_exit={last_exit}).",
                }

        return {
            "installed": True,
            "loaded": False,
            "pid": None,
            "last_exit": None,
            "installed_times": installed_times,
            "message": f"Plist installed at {PLIST_PATH} but not currently loaded by launchd.",
        }

    def logs(self, log_dir: Path, tail_n: int = 50) -> str:
        """
        Return recent log output from run_history.jsonl and launchd log files.

        Parameters
        ----------
        log_dir:
            Directory containing ``run_history.jsonl`` and launchd logs.
        tail_n:
            Number of lines to show from each file.

        Returns
        -------
        str
            Formatted log output.
        """
        import json

        output_parts: list[str] = []

        # run_history.jsonl
        jsonl_path = log_dir / "run_history.jsonl"
        if jsonl_path.exists():
            try:
                lines = jsonl_path.read_text(encoding="utf-8").splitlines()
                recent = lines[-tail_n:]
                output_parts.append(
                    f"=== run_history.jsonl (last {len(recent)} entries) ==="
                )
                for line in recent:
                    try:
                        data = json.loads(line)
                        output_parts.append(
                            f"  {data.get('ts', '?')[:19]}  status={data.get('status', '?')}  "
                            f"rows={data.get('rows_downloaded', '?')}  "
                            f"{data.get('message', '')[:80]}"
                        )
                    except json.JSONDecodeError:
                        output_parts.append(f"  {line[:120]}")
            except Exception as exc:
                output_parts.append(f"run_history.jsonl: could not read ({exc})")
        else:
            output_parts.append("run_history.jsonl: not found.")

        # launchd stderr log
        err_log = log_dir / "launchd.err.log"
        if err_log.exists():
            try:
                lines = err_log.read_text(
                    encoding="utf-8", errors="replace"
                ).splitlines()
                recent = lines[-tail_n:]
                output_parts.append(
                    f"\n=== launchd.err.log (last {len(recent)} lines) ==="
                )
                output_parts.extend(f"  {ln}" for ln in recent)
            except Exception as exc:
                output_parts.append(f"launchd.err.log: could not read ({exc})")

        return "\n".join(output_parts) if output_parts else "No logs found."

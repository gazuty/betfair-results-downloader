"""
scheduler/installers/taskscheduler.py
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Windows Task Scheduler installer (Phase 3.2).

Generates a Task Scheduler XML with one ``CalendarTrigger`` per scheduled
time (primary + retries), then registers/removes the task via
``schtasks /Create`` / ``schtasks /Delete``.

Uses ``pythonw.exe`` (not ``python.exe``) to avoid a console window flash
when the scheduled task fires.
"""
from __future__ import annotations

import subprocess
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ...config import ScheduleConfig


TASK_NAME = "BetfairResultsScheduler"


def _pythonw_from(python_path: Path) -> Path:
    """
    Return the ``pythonw.exe`` counterpart for the given ``python.exe`` path.
    Falls back to the original path if ``pythonw.exe`` is not found.
    """
    candidate = python_path.parent / "pythonw.exe"
    return candidate if candidate.exists() else python_path


def build_task_xml(
    schedule_cfg: ScheduleConfig,
    repo_root: Path,
    python_path: Path,
) -> str:
    """
    Build a Windows Task Scheduler XML string with one ``CalendarTrigger``
    per scheduled time.

    Parameters
    ----------
    schedule_cfg:
        Schedule configuration (source of times).
    repo_root:
        Repository root directory (working directory for the task).
    python_path:
        Python interpreter path (prefer ``pythonw.exe``).

    Returns
    -------
    str
        UTF-8 Task Scheduler XML as a string.
    """
    ns = "http://schemas.microsoft.com/windows/2004/02/mit/task"
    ET.register_namespace("", ns)

    def _el(tag: str) -> ET.Element:
        return ET.Element(f"{{{ns}}}{tag}")

    def _sub(parent: ET.Element, tag: str, text: str | None = None) -> ET.Element:
        el = ET.SubElement(parent, f"{{{ns}}}{tag}")
        if text is not None:
            el.text = text
        return el

    root = _el("Task")
    root.set("version", "1.2")

    # Registration info
    reg = _sub(root, "RegistrationInfo")
    _sub(reg, "Description", "Betfair Results daily download scheduler")
    _sub(reg, "Date", datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"))

    # Triggers
    triggers = _sub(root, "Triggers")
    all_times = [schedule_cfg.primary_time] + list(schedule_cfg.retry_times)
    seen: set[str] = set()
    for i, t in enumerate(all_times):
        t = t.strip()
        if not t or t in seen:
            continue
        seen.add(t)
        trigger = _sub(triggers, "CalendarTrigger")
        _sub(trigger, "StartBoundary", f"2000-01-01T{t}:00")
        _sub(trigger, "Enabled", "true")
        sched = _sub(trigger, "ScheduleByDay")
        _sub(sched, "DaysInterval", "1")

    # Settings
    settings = _sub(root, "Settings")
    _sub(settings, "MultipleInstancesPolicy", "IgnoreNew")
    _sub(settings, "DisallowStartIfOnBatteries", "false")
    _sub(settings, "StopIfGoingOnBatteries", "false")
    _sub(settings, "ExecutionTimeLimit", "PT4H")
    _sub(settings, "Enabled", "true")

    # Actions
    actions = _sub(root, "Actions")
    actions.set("Context", "Author")
    exec_el = _sub(actions, "Exec")
    _sub(exec_el, "Command", str(_pythonw_from(python_path)))
    _sub(exec_el, "Arguments", "-m betfair_results_downloader run")
    _sub(exec_el, "WorkingDirectory", str(repo_root))

    # Principals (run only when logged on)
    principals = _sub(root, "Principals")
    principal = _sub(principals, "Principal")
    principal.set("id", "Author")
    _sub(principal, "LogonType", "InteractiveToken")
    _sub(principal, "RunLevel", "LeastPrivilege")

    tree = ET.ElementTree(root)
    ET.indent(tree, space="  ")
    from io import BytesIO
    buf = BytesIO()
    tree.write(buf, encoding="utf-16", xml_declaration=True)
    return buf.getvalue().decode("utf-16")


class TaskSchedulerInstaller:
    """
    Windows Task Scheduler installer.

    Wraps ``schtasks.exe`` CLI commands.  All launchctl-equivalent calls
    are abstracted; ``dry_run=True`` skips the actual ``schtasks`` invocation.
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
        xml_content = build_task_xml(schedule_cfg, repo_root, py)

        xml_path = repo_root / "outputs" / "betfair_scheduler.xml"
        if log_dir:
            log_dir.mkdir(parents=True, exist_ok=True)
            xml_path = log_dir / "betfair_scheduler.xml"
        xml_path.parent.mkdir(parents=True, exist_ok=True)
        xml_path.write_text(xml_content, encoding="utf-16")

        if dry_run:
            return {
                "ok": True,
                "xml_path": str(xml_path),
                "message": f"Task XML written (dry-run, schtasks not called): {xml_path}",
            }

        result = subprocess.run(
            ["schtasks", "/Create", "/XML", str(xml_path),
             "/TN", TASK_NAME, "/F"],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            return {
                "ok": False,
                "xml_path": str(xml_path),
                "message": (
                    f"XML written to {xml_path} but schtasks failed "
                    f"(exit {result.returncode}): {result.stderr.strip()}"
                ),
            }
        return {
            "ok": True,
            "xml_path": str(xml_path),
            "message": f"Installed Task Scheduler task '{TASK_NAME}'.",
        }

    def uninstall(self, dry_run: bool = False) -> dict[str, Any]:
        if dry_run:
            return {"ok": True, "message": f"Would delete task '{TASK_NAME}' (dry-run)."}
        result = subprocess.run(
            ["schtasks", "/Delete", "/TN", TASK_NAME, "/F"],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            return {
                "ok": False,
                "message": f"schtasks /Delete failed: {result.stderr.strip()}",
            }
        return {"ok": True, "message": f"Task '{TASK_NAME}' deleted."}

    def status(self) -> dict[str, Any]:
        result = subprocess.run(
            ["schtasks", "/Query", "/TN", TASK_NAME, "/FO", "LIST"],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            return {
                "installed": False,
                "loaded": False,
                "pid": None,
                "last_exit": None,
                "message": f"Task '{TASK_NAME}' not found.",
            }
        return {
            "installed": True,
            "loaded": True,
            "pid": None,
            "last_exit": None,
            "message": result.stdout.strip(),
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
        return "\n".join(output_parts) if output_parts else "No logs found."

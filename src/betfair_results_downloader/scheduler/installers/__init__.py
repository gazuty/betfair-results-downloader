"""
scheduler/installers
~~~~~~~~~~~~~~~~~~~~
Platform-dispatching installer package for the Betfair Results scheduler
(Phase 3.1+).

:func:`get_installer` returns the correct backend for the current platform.
Each backend implements ``install``, ``uninstall``, ``status``, and ``logs``
with a consistent interface so ``__main__.py`` doesn't need to know which
platform it's on.
"""
from __future__ import annotations

import platform
import sys
from typing import TYPE_CHECKING


def get_installer():
    """
    Return the appropriate scheduler installer for the current platform.

    - macOS → :mod:`.launchd`
    - Windows → :mod:`.taskscheduler`
    - Linux with systemd → :mod:`.systemd_user`
    - Linux without systemd → :mod:`.cron`

    Raises ``RuntimeError`` on unsupported platforms.
    """
    system = platform.system()

    if system == "Darwin":
        from .launchd import LaunchdInstaller
        return LaunchdInstaller()

    if system == "Windows":
        from .taskscheduler import TaskSchedulerInstaller
        return TaskSchedulerInstaller()

    if system == "Linux":
        # Prefer systemd --user if available, fall back to cron
        if _systemd_available():
            from .systemd_user import SystemdUserInstaller
            return SystemdUserInstaller()
        from .cron import CronInstaller
        return CronInstaller()

    raise RuntimeError(
        f"No scheduler installer available for platform: {system}. "
        "Supported platforms: macOS (launchd), Windows (Task Scheduler), "
        "Linux (systemd --user or cron)."
    )


def _systemd_available() -> bool:
    """Return True if systemd --user appears to be available on this Linux system."""
    import shutil
    return shutil.which("systemctl") is not None

"""
scheduler/installers
~~~~~~~~~~~~~~~~~~~~
Platform-dispatching installer package for the Betfair Results scheduler.

:func:`get_installer` returns the backend for the current platform.

Only launchd is implemented. Backends for Windows Task Scheduler, systemd
--user and cron existed but were never run: this is a single-user macOS
tool, and carrying ~1,000 lines of untested cross-platform code cost more
than it bought -- the run-history renderer alone was copy-pasted into all
four and had already drifted between copies. They are recoverable from git
history if another platform is ever needed.
"""

from __future__ import annotations

import platform


def get_installer():
    """
    Return the scheduler installer for the current platform.

    Raises ``RuntimeError`` on anything but macOS.
    """
    system = platform.system()

    if system == "Darwin":
        from .launchd import LaunchdInstaller

        return LaunchdInstaller()

    raise RuntimeError(
        f"No scheduler installer available for platform: {system}. "
        "Only macOS (launchd) is supported; see the package docstring."
    )

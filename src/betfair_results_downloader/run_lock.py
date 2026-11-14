"""
One writing command at a time per results directory.

``run``, ``backfill``, ``backfill-commission`` and ``export-market-results``
each read a file, change it in memory and replace it: the canonical, the
status and commission stores, the results feed. Two of them overlapping (a
manual backfill still running when the 06:00 job fires) would each write
back what it had read, and the later replacement would silently drop the
other's rows. The canonical's "no betId may disappear" guard cannot see
that, because each process compares against its own read. The stores also
share one fixed temp-file name per store, so one process's cleanup can
delete the file the other is about to rename.

The lock is an advisory ``flock`` on ``<results_dir>/.pipeline.lock``. It
belongs to the open file, so it is released when the process exits or is
killed and there is no stale-lock case to clean up. The file's contents
only say who holds it, for the message a refused command prints.

Not covered, deliberately: the hourly ``racecard`` job, which writes only
the racecard store under its own lock and must not queue behind a long
backfill, and ``dm-report``, which only reads files that are replaced
atomically.
"""

from __future__ import annotations

import logging
import os
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterator

logger = logging.getLogger(__name__)

# Imported defensively, as in racecard: Windows has no fcntl, and the
# modules that import this one must still load there.
try:  # pragma: no cover - which branch runs depends on the platform
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]

LOCK_FILENAME = ".pipeline.lock"

# How often a waiting command looks again.
POLL_SECONDS = 2.0


class PipelineBusy(RuntimeError):
    """Another command holds the pipeline lock for this results directory."""


def _read_holder(handle) -> str:
    """Who the lock file says holds it; a fixed phrase when it says nothing."""
    try:
        handle.seek(0)
        note = handle.read().strip()
    except OSError:
        note = ""
    return note or "another downloader command"


@contextmanager
def pipeline_lock(
    results_dir: Path,
    *,
    command: str,
    wait_seconds: float = 0.0,
    sleep: Callable[[float], object] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> Iterator[None]:
    """
    Hold the pipeline lock for ``results_dir`` while the block runs.

    ``command`` names the caller in the lock file ("run", "backfill"), so a
    refused command can say what it is waiting behind. With
    ``wait_seconds`` of zero a busy lock raises :class:`PipelineBusy` at
    once, which is right for a command a person just typed. The unattended
    scheduled run passes a wait instead, so a short manual command that
    happens to overlap it delays the run rather than failing it.

    Where locking is unavailable (no ``fcntl``, a lock file that cannot be
    opened, or a filesystem that refuses ``flock``) the block runs unlocked and says so: the lock guards
    against a rare overlap and must never be what stops a run.
    """
    path = Path(results_dir) / LOCK_FILENAME
    if fcntl is None:
        logger.warning(
            "No file locking on this platform; %s runs without the pipeline "
            "lock. Do not run two downloader commands at once.",
            command,
        )
        yield
        return

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(path, "a+", encoding="utf-8")
    except OSError as exc:
        # A results directory that cannot hold a lock file (read-only, wrong
        # owner) will fail the command's own writes with a clearer error;
        # the lock is not the place to report it.
        logger.warning(
            "Could not open %s (%s: %s); %s runs without the pipeline lock. "
            "Do not run two downloader commands at once.",
            path,
            type(exc).__name__,
            exc,
            command,
        )
        yield
        return

    with handle:
        deadline = monotonic() + max(wait_seconds, 0.0)
        announced = False
        locked = False
        while True:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
                break
            except BlockingIOError:
                holder = _read_holder(handle)
                if monotonic() >= deadline:
                    waited = (
                        f" after waiting {wait_seconds:.0f}s" if wait_seconds else ""
                    )
                    raise PipelineBusy(
                        f"{command} did not start{waited}: the pipeline lock "
                        f"{path} is held by {holder}. Two commands writing the "
                        f"same results directory would overwrite each other; "
                        f"run this again when that one has finished."
                    ) from None
                if not announced:
                    logger.warning(
                        "%s is waiting up to %.0fs for the pipeline lock, held by %s.",
                        command,
                        wait_seconds,
                        holder,
                    )
                    announced = True
                sleep(POLL_SECONDS)
            except OSError as exc:
                logger.warning(
                    "Could not lock %s (%s: %s); %s runs without the pipeline "
                    "lock. Do not run two downloader commands at once.",
                    path,
                    type(exc).__name__,
                    exc,
                    command,
                )
                break
        try:
            if locked:
                _write_note(
                    handle,
                    f"pid {os.getpid()} ({command}, started "
                    f"{datetime.now(timezone.utc):%Y-%m-%dT%H:%M:%SZ})",
                )
            yield
        finally:
            if locked:
                _write_note(handle, "")
                fcntl.flock(handle, fcntl.LOCK_UN)


def _write_note(handle, note: str) -> None:
    """Replace the lock file's contents; the note is a courtesy, never fatal."""
    try:
        handle.seek(0)
        handle.truncate()
        handle.write(note)
        handle.flush()
    except OSError as exc:
        logger.warning("Could not update the pipeline lock note (%s).", exc)

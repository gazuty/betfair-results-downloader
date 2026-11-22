"""
scheduler/runner.py
~~~~~~~~~~~~~~~~~~~
Headless scheduled download runner.

Scheduled runs now use timestamp-based incremental checkpoints so all four
configured daily run times can perform real download attempts.
"""

from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Optional

import pandas as pd

from ..config import ScheduleConfig
from ..error_text import brief_error
from ..paths import resolve_results_dir
from ..run_lock import PipelineBusy, pipeline_lock
from .auth import build_api_client
from .gap_detector import compute_backfill_window, derive_coverage_dates
from .state import append_run_history, upsert_schedule_state, write_today_success_marker
from .time_semantics import get_scheduler_now

logger = logging.getLogger(__name__)


@dataclass
class RunResult:
    ok: bool
    status: str = ""
    from_date: Optional[date] = None
    to_date: Optional[date] = None
    rows_downloaded: int = 0
    rows_in_canonical: int = 0
    azure_published: bool = False
    message: str = ""
    errors: list[str] = field(default_factory=list)
    from_dt_utc: Optional[datetime] = None
    to_dt_utc: Optional[datetime] = None
    last_confirmed_settled_at_utc: Optional[datetime] = None
    download_started_utc: Optional[datetime] = None
    download_finished_utc: Optional[datetime] = None
    # True when the attempt did not deliver: it failed, or it succeeded
    # without a fresh status check, commission read or results feed. The
    # hourly retry job runs the pipeline again while the latest attempts
    # say so (see scheduler.retry).
    retry_wanted: bool = False
    # True when the pipeline did not start because another downloader
    # command held the lock.
    busy: bool = False
    # True when a retry found, once it held the lock, that it was no longer
    # due (another attempt had delivered) or could not claim its attempt.
    superseded: bool = False
    # False when the attempt could not be written to run_history.jsonl.
    # The hourly retry reads only that file, so such an attempt is not
    # retried and the alert must not promise that it will be.
    history_recorded: bool = True


# Disk thresholds for the preflight. The pipeline rewrites a ~270MB canonical
# every run; writing it onto a full disk is the one moment damage is possible,
# and the 2026-08-30/31 incidents both began as a full disk nothing reported.
# How long the unattended scheduled run waits for another command to finish
# before giving up. A manual export or commission backfill takes seconds to
# a few minutes; a full run about two. Past this the run fails and says so,
# and the next scheduled run covers the same window through gap detection.
SCHEDULED_LOCK_WAIT_SECONDS = 600.0

DISK_HARD_FLOOR_BYTES = 2 * 1024**3
DISK_SOFT_FLOOR_BYTES = 10 * 1024**3


def check_disk_space(
    results_dir: str | Path,
    *,
    hard_floor: int = DISK_HARD_FLOOR_BYTES,
    soft_floor: int = DISK_SOFT_FLOOR_BYTES,
) -> tuple[bool, Optional[str]]:
    """
    Return (ok_to_run, warning).

    ok_to_run False means below the hard floor: refuse before touching the
    canonical. A warning string means below the soft floor: run, but say so
    on the same Slack path as everything else, days before it is an incident.
    Unreadable filesystems return ok with no warning -- the preflight must
    never be the thing that stops a run.
    """
    # The directory may not exist yet (first run) or may have been removed or
    # evicted -- exactly the situations where the disk is most suspect. Walk
    # to the nearest existing ancestor rather than waving the run through.
    probe = Path(str(results_dir) or ".").expanduser()
    while not probe.exists() and probe.parent != probe:
        probe = probe.parent
    try:
        usage = shutil.disk_usage(str(probe))
    except OSError:
        return True, None

    free_gb = usage.free / 1024**3
    if usage.free < hard_floor:
        return False, (
            f"Disk critically low: {free_gb:.1f} GB free. Refusing to rewrite "
            f"the canonical onto a nearly full disk."
        )
    if usage.free < soft_floor:
        return True, f"⚠️ Disk low: {free_gb:.1f} GB free."
    return True, None


def azure_state_configured(creds: dict[str, Any]) -> bool:
    """
    True when an Azure checkpoint is expected to be written.

    ``upsert_schedule_state`` also returns False when Azure was never
    configured, which is a supported CSV-only setup -- gap detection falls
    back to the canonical CSV. Only a configured-but-unreachable Azure is a
    problem worth downgrading a run for; without this distinction every
    successful CSV-only run would alert.
    """
    return bool((creds.get("azure_sql") or {}).get("server"))


def apply_azure_state_outcome(
    result: "RunResult", creds: dict[str, Any], state_written: bool
) -> "RunResult":
    """
    Downgrade ``result`` when a configured Azure checkpoint failed to write.

    The checkpoint is what stops the next run re-downloading the same window,
    so silently continuing means every future run repeats this one while still
    reporting success.
    """
    if azure_state_configured(creds) and not state_written:
        result.status = "partial"
        reason = (
            "Azure ScheduleState checkpoint was not written; "
            "the next run will repeat this window."
        )
        result.errors.append(reason)
        result.message = f"{result.message} {reason}".strip()
    return result


def _azure_publish_allowed(creds: dict[str, Any], schedule_cfg: ScheduleConfig) -> bool:
    user = creds.get("user") or {}
    enable_azure = bool(user.get("enable_azure_sql", False))
    dry_run = bool(user.get("dry_run", True))
    return (
        enable_azure
        and not dry_run
        and schedule_cfg.publish_to_azure
        and schedule_cfg.allow_azure_publish
    )


def _resolve_repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _resolve_results_dir(creds: dict[str, Any]) -> Path:
    return resolve_results_dir(creds)


def _preflight_disk(creds: dict[str, Any]) -> tuple[bool, Optional[str]]:
    """
    Resolve the results directory and run the disk preflight against it.

    A malformed ``paths`` section is not a disk problem: a resolution error
    here skips the preflight so the pipeline itself fails with the real
    configuration error, converted into a failed :class:`RunResult` exactly
    as it was before the preflight existed.
    """
    try:
        results_dir = _resolve_results_dir(creds)
    except Exception:
        return True, None
    return check_disk_space(results_dir)


def _resolve_log_dir(creds: dict[str, Any], schedule_cfg: ScheduleConfig) -> Path:
    if schedule_cfg.log_dir:
        return Path(schedule_cfg.log_dir).expanduser()
    repo_root = _resolve_repo_root()
    return repo_root / "outputs"


def _extract_max_settled_at_utc(df: pd.DataFrame) -> Optional[datetime]:
    if df is None or df.empty or "settledDate" not in df.columns:
        return None
    ts = pd.to_datetime(
        df["settledDate"], utc=True, errors="coerce", format="ISO8601"
    ).dropna()
    if ts.empty:
        return None
    max_ts = ts.max().to_pydatetime()
    if max_ts.tzinfo is None:
        max_ts = max_ts.replace(tzinfo=timezone.utc)
    return max_ts.astimezone(timezone.utc)


def _log_step_failure(warning: str, exc: BaseException) -> None:
    """
    Log a failed step: the short warning that goes into the run message, and
    the exception in full. The message reaches Slack and the run history, so
    it carries :func:`~..error_text.brief_error`; the parameters and response
    body a Betfair error drags along belong in the log only.
    """
    logger.warning(warning)
    logger.warning("Full error for the warning above: %s: %s", type(exc).__name__, exc)


def _refresh_market_status(
    client: Any,
    results_dir: Path,
    df_window: Optional[pd.DataFrame],
    df_canonical: Optional[pd.DataFrame],
    status_cb: Optional[Any] = None,
) -> Optional[str]:
    """
    Record settlement status for this run's markets (see market_status).

    Runs on every pipeline run, including one whose download returned no
    rows: a pending outright must keep being asked about until Betfair says
    CLOSED, and the run that finally sees the close is typically a quiet one
    -- all of the user's bets settled long before the market itself did.
    ``df_canonical`` may be None on that path; the seed then reads what it
    needs from the canonical on disk.

    Supplementary like enrichment: never raises. Returns a ⚠️ warning string
    on failure so the caller can carry it on the run message, where _cmd_run
    posts it to Slack as "succeeded with a warning". Until the next run
    re-checks, the report counts this window's markets as final.
    """
    try:
        from ..downloader_core import resolve_enrichment_cache_dir  # noqa: PLC0415
        from ..market_status import (  # noqa: PLC0415
            load_canonical_market_dates,
            update_market_status,
        )

        if df_canonical is None:
            # No canonical in hand (nothing was downloaded, so nothing was
            # written): read the two columns the seed needs from disk, so the
            # first run after deploy and the self-heal after a failed step
            # both work on a quiet window too.
            df_canonical = load_canonical_market_dates(results_dir)
        ms = update_market_status(
            client=client,
            cache_dir=resolve_enrichment_cache_dir(results_dir),
            df_window=df_window,
            df_canonical=df_canonical,
            status_cb=status_cb,
        )
        logger.info("Market status result: %s", ms.message)
        return None
    except Exception as exc:
        warning = (
            f"⚠️ Market settlement status check failed "
            f"({brief_error(exc)}); the report will treat this "
            f"window's markets as fully settled until the next run."
        )
        _log_step_failure(warning, exc)
        return warning


def _refresh_market_commission(
    client: Any,
    results_dir: Path,
    from_dt_utc: Optional[datetime],
    to_dt_utc: Optional[datetime],
    chunk_days: int,
    df_canonical: Optional[pd.DataFrame],
    status_cb: Optional[Any] = None,
) -> Optional[str]:
    """
    Record what Betfair charged on this run's markets (see commission).

    Runs after the status step on every pipeline run, including a quiet
    one: the read that finally sees a long-open outright's commission is
    the run after the status step saw it CLOSED, and that run usually has
    nothing to download. Reads the status file the step before just wrote,
    so the pre-close placeholders it re-queries are chosen on fresh data.
    ``df_canonical`` feeds the recent-market seed that re-reads whatever an
    earlier failed step missed; None on a quiet run, when the two columns
    the seed needs are read from disk instead.

    Supplementary like the status step: never raises. Returns a ⚠️ warning
    string on failure so the run message carries it to Slack; until a later
    run re-reads them, the report shows those markets as commission unknown.
    """
    try:
        from ..commission import update_market_commission  # noqa: PLC0415
        from ..downloader_core import resolve_enrichment_cache_dir  # noqa: PLC0415
        from ..market_status import (  # noqa: PLC0415
            load_canonical_market_dates,
            load_market_status,
            resolve_status_path,
        )

        cache_dir = resolve_enrichment_cache_dir(results_dir)
        if df_canonical is None:
            df_canonical = load_canonical_market_dates(results_dir)
        try:
            df_status = load_market_status(resolve_status_path(cache_dir))
        except Exception as exc:
            # The status step already warned about its file; without it the
            # window read still happens, only the pending re-queries wait.
            logger.warning(
                "Commission step could not read the status file (%s: %s); "
                "skipping pending re-queries this run.",
                type(exc).__name__,
                exc,
            )
            df_status = None
        cr = update_market_commission(
            client=client,
            cache_dir=cache_dir,
            from_dt=from_dt_utc,
            to_dt=to_dt_utc,
            df_status=df_status,
            df_canonical=df_canonical,
            chunk_days=chunk_days,
            status_cb=status_cb,
        )
        logger.info("Commission result: %s", cr.message)
        return None
    except Exception as exc:
        warning = (
            f"⚠️ Commission read failed ({brief_error(exc)}); the "
            f"report will show this window's markets as commission unknown "
            f"until a later run re-reads them."
        )
        _log_step_failure(warning, exc)
        return warning


# Long enough for a first full copy to a slow folder (a normal backup
# takes a second or two), short enough that a folder which has stopped
# responding delays the publish and the run's report by minutes at most.
_BACKUP_TARGET_TIME_LIMIT_SECONDS = 120.0


def _back_up(
    creds: dict[str, Any], results_dir: Path, say: Callable[[str], None]
) -> Optional[str]:
    """
    Copy the order snapshots, yearly archives and a bundle of the stores
    to every configured backup directory; None when there is none, or all
    of them are complete.

    Best-effort by design: a cloud folder being offline or full must never
    fail a run that just succeeded locally, but the warning rides the
    result message so the ⚠️ reaches Slack. Nor may one hold the run up:
    each directory gets ``_BACKUP_TARGET_TIME_LIMIT_SECONDS`` and is then
    left behind with a warning, because the publish and the run's own
    report still come after this step.
    """
    from ..backup import run_backup  # noqa: PLC0415
    from ..paths import (  # noqa: PLC0415
        resolve_backup_dirs,
        resolve_backup_extra_files,
        resolve_backup_retention,
    )

    try:
        backup_dirs = resolve_backup_dirs(creds)
        if not backup_dirs:
            return None
        user = creds.get("user", {}) or {}
        warning = run_backup(
            results_dir,
            backup_dirs,
            tiers=resolve_backup_retention(
                creds, int(user.get("snapshot_retention_days", 14))
            ),
            extra_files=resolve_backup_extra_files(creds),
            status_cb=say,
            target_time_limit=_BACKUP_TARGET_TIME_LIMIT_SECONDS,
        )
    except Exception as exc:
        # Resolving the settings can raise too ("~nosuchuser/x" has no
        # home to expand to), and by now the run's data is already written.
        warning = f"⚠️ Backup failed unexpectedly: {type(exc).__name__}: {exc}"
    if warning:
        logger.warning(warning)
    return warning


def _refresh_racecard(
    client: Any,
    results_dir: Path,
    status_cb: Optional[Any] = None,
) -> Optional[str]:
    """
    Capture the forward racecard so this run's upcoming racing markets keep
    their full website names (see racecard).

    Supplementary like the status step: never raises. Returns a ⚠️ warning
    on failure; the hourly racecard job and the next run capture again, and
    until then a market that settles unseen is named from the fallback.
    """
    try:
        from ..commission import (  # noqa: PLC0415
            load_market_commission,
            resolve_commission_path,
        )
        from ..csv_utils import decimal_key  # noqa: PLC0415
        from ..downloader_core import resolve_enrichment_cache_dir  # noqa: PLC0415
        from ..racecard import update_racecard  # noqa: PLC0415

        cache_dir = resolve_enrichment_cache_dir(results_dir)
        store = load_market_commission(resolve_commission_path(cache_dir))
        rc = update_racecard(
            client=client,
            cache_dir=cache_dir,
            bet_market_keys=set(store["marketId"].map(decimal_key)),
            status_cb=status_cb,
        )
        logger.info("Racecard result: %s", rc.message)
        # Both leave racing names or countries incomplete, so both are said
        # out loud rather than left in the log.
        if rc.truncated_slices or rc.venue_warning:
            return f"⚠️ {rc.message}"
        return None
    except Exception as exc:
        warning = (
            f"⚠️ Racecard capture failed ({brief_error(exc)}); racing "
            f"markets that settle before the next capture are named from the "
            f"fallback, with country Unknown where the venue is ambiguous."
        )
        _log_step_failure(warning, exc)
        return warning


def _publish_market_results(
    results_dir: Path,
    status_cb: Optional[Any] = None,
    *,
    status_failed: bool = False,
) -> tuple[list[str], bool]:
    """
    Rebuild the results feed the dashboard reads (see market_results).

    Returns ``(warnings, rebuilt)``. ``rebuilt`` is False when the feed on
    disk is still the previous one, which is what makes the run ask for a
    retry; a warning alone (a market stuck without commission) does not.

    Runs last, after the status and commission steps, so it sees this
    run's settlements and closes. Never raises: on failure the previous
    feed stays in place and ages, and the returned ⚠️ warning reaches
    Slack. Also returns the feed's own warnings (a market stuck without
    commission).

    Skipped when this run's status step failed. The status file is then
    stale for this window, and a market that has just started settling in
    stages would be published as final; the previous feed is kept instead.
    """
    if status_failed:
        warning = (
            "⚠️ Results feed not rebuilt because the settlement status check "
            "failed; the dashboard keeps the previous feed until a run succeeds."
        )
        logger.warning(warning)
        return [warning], False
    try:
        from ..market_results import (  # noqa: PLC0415
            build_market_results_from_disk,
            write_market_results,
        )

        result = build_market_results_from_disk(results_dir)
        # This run has just downloaded, so the feed's lastDownloadUtc moves;
        # a rebuild from disk (export-market-results) leaves it where it was.
        path = write_market_results(
            results_dir, result, downloaded_at=datetime.now(timezone.utc)
        )
        logger.info("%s Written to %s.", result.message, path)
        if status_cb:
            status_cb(result.message)
        return list(result.warnings), True
    except Exception as exc:
        warning = (
            f"⚠️ Results feed not rebuilt ({brief_error(exc)}); the "
            f"dashboard keeps the previous feed until a run succeeds."
        )
        _log_step_failure(warning, exc)
        return [warning], False


def _data_steps_incomplete(
    status_warning: Optional[str],
    commission_warning: Optional[str],
    feed_rebuilt: bool,
) -> bool:
    """
    True when a run that downloaded fine still left the dashboard without
    fresh data: the settlement-status check or the commission read failed,
    or the results feed was not rebuilt. Another attempt can fix each of
    those. A racecard failure cannot be made good later (the hourly capture
    is its retry), nor can a disk or backup warning, so they do not ask.
    """
    return bool(status_warning or commission_warning or not feed_rebuilt)


def _run_pipeline(
    creds: dict[str, Any],
    schedule_cfg: ScheduleConfig,
    from_dt_utc: datetime,
    to_dt_utc: datetime,
    *,
    command: str = "run",
    lock_wait_seconds: float = 0.0,
    hold_lock: bool = True,
) -> RunResult:
    """
    Run the pipeline for one window: download, enrich, write the CSVs,
    record settlement status and commission, capture the racecard, rebuild
    the results feed and publish to Azure.

    Holds the pipeline lock throughout (see :mod:`..run_lock`), so a
    scheduled run and a manual command cannot overwrite each other's
    files. A lock still busy after ``lock_wait_seconds`` is a failed run
    that touched nothing. ``hold_lock=False`` is for a caller that already
    holds it (``run_scheduled``, which keeps it until the attempt is
    recorded).

    Never raises: any unhandled exception is converted into a failed
    :class:`RunResult` so the caller always records the attempt in
    ``run_history.jsonl``.
    """
    try:
        if not hold_lock:
            return _run_pipeline_inner(creds, schedule_cfg, from_dt_utc, to_dt_utc)
        with pipeline_lock(
            _resolve_results_dir(creds),
            command=command,
            wait_seconds=lock_wait_seconds,
        ):
            return _run_pipeline_inner(creds, schedule_cfg, from_dt_utc, to_dt_utc)
    except PipelineBusy as exc:
        logger.error("%s", exc)
        return RunResult(
            ok=False,
            status="failed",
            from_dt_utc=from_dt_utc,
            to_dt_utc=to_dt_utc,
            message=str(exc),
            retry_wanted=True,
            busy=True,
        )
    except Exception as exc:
        logger.exception("Scheduled pipeline failed with an unhandled error.")
        return RunResult(
            ok=False,
            status="failed",
            from_dt_utc=from_dt_utc,
            to_dt_utc=to_dt_utc,
            # logger.exception above has the full text and the traceback.
            message=f"Scheduled pipeline failed: {brief_error(exc)}",
            retry_wanted=True,
        )


def _run_pipeline_inner(
    creds: dict[str, Any],
    schedule_cfg: ScheduleConfig,
    from_dt_utc: datetime,
    to_dt_utc: datetime,
) -> RunResult:
    results_dir = _resolve_results_dir(creds)
    betfair_creds = creds.get("betfair") or {}
    run_started = datetime.now(timezone.utc)
    client = None

    try:
        logger.info("Authenticating with Betfair (cert-based)...")
        try:
            client = build_api_client(betfair_creds)
        except Exception as exc:
            msg = f"Betfair authentication failed: {brief_error(exc)}"
            logger.error("Betfair authentication failed: %s", exc)
            return RunResult(
                ok=False,
                status="failed",
                from_dt_utc=from_dt_utc,
                to_dt_utc=to_dt_utc,
                message=msg,
                retry_wanted=True,
            )

        from ..downloader_core import fetch_cleared_orders_df_range  # noqa: PLC0415

        logger.info(
            "Downloading cleared orders %s → %s (chunk_days=%d)...",
            from_dt_utc,
            to_dt_utc,
            schedule_cfg.chunk_days,
        )

        def _say(msg: str) -> None:
            logger.info(msg)

        dl = fetch_cleared_orders_df_range(
            betfair=betfair_creds,
            from_date=from_dt_utc,
            to_date=to_dt_utc,
            chunk_days=schedule_cfg.chunk_days,
            api_client=client,
            status_cb=_say,
        )
        logger.info("Download result: %s", dl.message)

        if not dl.attempted or dl.df_co is None or dl.df_co.empty:
            # Nothing was observed, so nothing new is confirmed: leave the
            # checkpoint alone (None means "keep the previous value" in
            # upsert_schedule_state) instead of asserting coverage to now.
            # Pending markets are still re-checked: the run that sees an
            # outright finally CLOSED is usually one with nothing to download.
            status_warning = _refresh_market_status(client, results_dir, None, None)
            commission_warning = _refresh_market_commission(
                client,
                results_dir,
                from_dt_utc,
                to_dt_utc,
                schedule_cfg.chunk_days,
                None,
            )
            racecard_warning = _refresh_racecard(client, results_dir)
            # A quiet run still rebuilds the feed: it is the run that sees an
            # outright close or a late commission land.
            feed_warnings, feed_rebuilt = _publish_market_results(
                results_dir, status_failed=status_warning is not None
            )
            empty_warnings = [
                w for w in (status_warning, commission_warning, racecard_warning) if w
            ] + feed_warnings
            warn_suffix = (" " + " ".join(empty_warnings)) if empty_warnings else ""
            return RunResult(
                ok=True,
                status="success",
                rows_downloaded=0,
                from_dt_utc=from_dt_utc,
                to_dt_utc=to_dt_utc,
                last_confirmed_settled_at_utc=None,
                download_started_utc=run_started,
                download_finished_utc=datetime.now(timezone.utc),
                message=f"Download returned no rows. {dl.message}{warn_suffix}",
                retry_wanted=_data_steps_incomplete(
                    status_warning, commission_warning, feed_rebuilt
                ),
            )

        df_co = dl.df_co

        from ..downloader_core import (
            enrich_with_market_catalogue,
            resolve_enrichment_cache_dir,
        )  # noqa: PLC0415

        logger.info("Enriching with market catalogue...")
        try:
            df_co, enr = enrich_with_market_catalogue(
                df_co=df_co,
                betfair=betfair_creds,
                cache_dir=resolve_enrichment_cache_dir(results_dir),
                enable=True,
                use_cache=True,
                api_client=client,
                status_cb=_say,
            )
            logger.info("Enrich result: %s", enr.message)
        except Exception as exc:
            # Enrichment is supplementary: keep the downloaded rows and let
            # a future run backfill names from the cache.
            logger.warning(
                "Enrichment failed (%s: %s); continuing with unenriched rows.",
                type(exc).__name__,
                exc,
            )

        from ..downloader_core import write_csv_outputs  # noqa: PLC0415

        logger.info("Writing CSV outputs to %s...", results_dir)
        user = creds.get("user", {}) or {}
        csvr = write_csv_outputs(
            df_co=df_co,
            results_csv_dir=results_dir,
            status_cb=_say,
            snapshot_retention=int(user.get("snapshot_retention_days", 14)),
            compress_snapshots=bool(user.get("compress_snapshots", True)),
            archive_months=int(user.get("canonical_archive_months", 12)),
        )
        logger.info("CSV result: %s", csvr.message)

        # Settlement status for every market this window touched, recorded
        # while Betfair still answers for them. This is what lets the daily
        # report hold back partially settled outrights; see market_status.
        status_warning = _refresh_market_status(
            client, results_dir, df_co, csvr.df_canonical, _say
        )

        # What Betfair charged on those markets, read after the status step so
        # the pre-close placeholders it re-queries are chosen on fresh data.
        commission_warning = _refresh_market_commission(
            client,
            results_dir,
            from_dt_utc,
            to_dt_utc,
            schedule_cfg.chunk_days,
            csvr.df_canonical,
            _say,
        )

        racecard_warning = _refresh_racecard(client, results_dir, _say)
        feed_warnings, feed_rebuilt = _publish_market_results(
            results_dir, _say, status_failed=status_warning is not None
        )

        # One-way backup, last: the state bundle must hold the stores as
        # this run left them, and the feed step is the last to read them.
        backup_warning = _back_up(creds, results_dir, _say)

        run_warnings = [
            w
            for w in (
                backup_warning,
                status_warning,
                commission_warning,
                racecard_warning,
            )
            if w
        ] + feed_warnings
        warn_suffix = (" " + " ".join(run_warnings)) if run_warnings else ""

        max_settled_at_utc = _extract_max_settled_at_utc(df_co)

        azure_published = False
        if _azure_publish_allowed(creds, schedule_cfg):
            logger.info("Azure publish gates open, publishing...")
            try:
                from ..downloader_core import (  # noqa: PLC0415
                    prepare_azure_dataset,
                    select_azure_rows_from_canonical,
                )
                from ..azure_publish import publish_to_azure_sql  # noqa: PLC0415

                # Split settlements: aggregate each touched market from the
                # canonical, not just this window's rows -- see
                # select_azure_rows_from_canonical.
                prep = prepare_azure_dataset(
                    df_co=select_azure_rows_from_canonical(
                        csvr.df_canonical,
                        df_co,
                        results_csv_dir=results_dir,
                        archive_months=int(user.get("canonical_archive_months", 12)),
                    )
                )
                if not prep.attempted:
                    # A failed preparation (e.g. unparseable profits) is an
                    # Azure failure, not an empty publish: falling through
                    # would report success and advance the checkpoint with
                    # the affected markets silently stale.
                    raise RuntimeError(prep.message or "Azure prep failed.")
                if prep.rows_to_write:
                    az = publish_to_azure_sql(
                        creds=creds,
                        rows_to_write=prep.rows_to_write,
                        dry_run=False,
                    )
                    logger.info("Azure publish result: %s", az.message)
                    if not az.ok:
                        # publish_to_azure_sql reports its own failures in the
                        # result rather than raising — treat them as partial.
                        raise RuntimeError(az.message or "Azure publish failed.")
                    azure_published = az.attempted
                else:
                    logger.info(
                        "Azure prep result: %s (nothing to write)", prep.message
                    )
            except Exception as exc:
                logger.warning("Azure publish failed: %s", exc)
                return RunResult(
                    ok=True,
                    status="partial",
                    retry_wanted=True,
                    rows_downloaded=dl.rows_downloaded,
                    rows_in_canonical=csvr.rows_in_canonical,
                    azure_published=False,
                    from_dt_utc=from_dt_utc,
                    to_dt_utc=to_dt_utc,
                    last_confirmed_settled_at_utc=max_settled_at_utc,
                    download_started_utc=run_started,
                    download_finished_utc=datetime.now(timezone.utc),
                    message=(
                        f"CSV written ({csvr.rows_in_canonical:,} rows canonical). "
                        f"Azure publish failed: {brief_error(exc)}{warn_suffix}"
                    ),
                )
        else:
            gates = []
            user = creds.get("user") or {}
            if not user.get("enable_azure_sql"):
                gates.append("user.enable_azure_sql=false")
            if user.get("dry_run", True):
                gates.append("user.dry_run=true")
            if not schedule_cfg.publish_to_azure:
                gates.append("schedule.publish_to_azure=false")
            if not schedule_cfg.allow_azure_publish:
                gates.append("schedule.allow_azure_publish=false")
            logger.info(
                "Azure publish skipped (%s).", ", ".join(gates) or "gates closed"
            )

        run_finished = datetime.now(timezone.utc)
        elapsed = (run_finished - run_started).total_seconds()
        summary = (
            f"Scheduled run complete: {dl.rows_downloaded:,} rows downloaded, "
            f"{csvr.rows_in_canonical:,} rows in canonical, "
            f"azure={'published' if azure_published else 'skipped'}. "
            f"Elapsed {elapsed:.1f}s.{warn_suffix}"
        )
        logger.info(summary)

        return RunResult(
            ok=True,
            status="success",
            rows_downloaded=dl.rows_downloaded,
            rows_in_canonical=csvr.rows_in_canonical,
            azure_published=azure_published,
            message=summary,
            from_dt_utc=from_dt_utc,
            to_dt_utc=to_dt_utc,
            last_confirmed_settled_at_utc=max_settled_at_utc,
            download_started_utc=run_started,
            download_finished_utc=run_finished,
            retry_wanted=_data_steps_incomplete(
                status_warning, commission_warning, feed_rebuilt
            ),
        )

    finally:
        if client is not None:
            try:
                client.logout()
                logger.debug("Betfair logout clean.")
            except Exception as exc:
                logger.warning("Betfair logout raised: %s", exc)


def run_scheduled(
    creds: dict[str, Any],
    schedule_cfg: ScheduleConfig,
    *,
    claim: Optional[Callable[[], bool]] = None,
    attempt_id: Optional[str] = None,
) -> RunResult:
    """
    One scheduled attempt: preflight, gap detection, the pipeline, then the
    checkpoint and the history record.

    ``attempt_id`` is written on every history record this attempt makes.
    A retry passes the id its claim carries, which is how the claim and the
    outcome are recognised as one attempt.

    Everything from the disk preflight to the history record happens under
    the pipeline lock. Two attempts can start together (launchd starts a
    missed scheduled run and a retry at once after a wake), and each of
    these would go wrong outside it:

    - The window is computed from the checkpoint. Computed before waiting
      for the lock, it would be the window the other attempt has just
      downloaded; computed after, it starts where that attempt stopped.
    - The history record must be written before the lock is released, or
      the other attempt could start while this one's result was unknown.

    ``claim`` makes this a retry (see :mod:`.retry`):

    - It does not wait for the lock. If another downloader command holds
      it, that command is the attempt, and a ``busy`` result is returned
      with nothing recorded.
    - With the lock held, and before anything else can record a failure,
      it calls ``claim()``, which looks at the history again and records
      that the retry has started. If that returns False the retry is no
      longer due, or could not be counted, and a ``superseded`` result is
      returned without running the pipeline.
    """
    from .retry import KIND_RETRY, KIND_SCHEDULED  # noqa: PLC0415

    is_retry = claim is not None
    kind = KIND_RETRY if is_retry else KIND_SCHEDULED
    command = "retry" if is_retry else "run"
    scheduler_now = get_scheduler_now(schedule_cfg)
    today_local = scheduler_now.today_local
    today_utc = scheduler_now.today_utc
    log_dir = _resolve_log_dir(creds, schedule_cfg)
    run_started = scheduler_now.now_utc

    def record_refusal(result: RunResult) -> RunResult:
        """Record an attempt that never reached the pipeline."""
        result.history_recorded = append_run_history(
            str(log_dir),
            {
                "kind": kind,
                "attempt_id": attempt_id,
                "retry_wanted": True,
                "status": result.status,
                "message": result.message,
                "run_started": run_started.isoformat(),
                "run_finished": datetime.now(timezone.utc).isoformat(),
                "today_local": today_local.isoformat(),
                "today_utc": today_utc.isoformat(),
            },
        )
        return result

    def attempt(*, lock_held: bool) -> RunResult:
        # The resolved directory, not the raw config: an empty
        # results_csv_dir falls back to the OneDrive default, which can sit
        # on a different filesystem from the working directory.
        disk_ok, disk_warning = _preflight_disk(creds)
        if not disk_ok:
            logger.error(disk_warning)
            # The refusal is a failure mode of its own; it belongs in the
            # operational record alongside every other scheduled failure.
            return record_refusal(
                RunResult(
                    ok=False, status="failed", message=disk_warning, retry_wanted=True
                )
            )

        logger.info("Computing incremental backfill window...")
        from_dt_utc, to_dt_utc, gap_reason = compute_backfill_window(
            creds, schedule_cfg
        )
        logger.info("Backfill window: %s → %s (%s)", from_dt_utc, to_dt_utc, gap_reason)

        if lock_held:
            result = _run_pipeline(
                creds, schedule_cfg, from_dt_utc, to_dt_utc, hold_lock=False
            )
        else:
            result = _run_pipeline(
                creds, schedule_cfg, from_dt_utc, to_dt_utc, command=command
            )

        if disk_warning:
            # Attached before state and history are written: the operational
            # record must carry the same warning the operator is alerted with
            # -- a later incident investigation reads run_history.jsonl, not
            # Slack.
            result.message = f"{result.message} {disk_warning}".strip()
        result.from_dt_utc = from_dt_utc
        result.to_dt_utc = to_dt_utc
        result.from_date = from_dt_utc.date()
        result.to_date = to_dt_utc.date()

        run_finished = datetime.now(timezone.utc)

        if result.ok and result.status == "success":
            covered_utc, covered_local = derive_coverage_dates(
                from_dt_utc, to_dt_utc, schedule_cfg.timezone
            )
            state_written = upsert_schedule_state(
                creds,
                last_covered_date_utc=covered_utc,
                last_covered_date_local=covered_local,
                last_covered_timezone=schedule_cfg.timezone,
                status="success",
                message=result.message,
                run_started_utc=run_started,
                run_finished_utc=run_finished,
                last_confirmed_settled_at_utc=result.last_confirmed_settled_at_utc,
                last_successful_download_started_utc=result.download_started_utc,
                last_successful_download_finished_utc=result.download_finished_utc,
            )
            apply_azure_state_outcome(result, creds, state_written)
            if result.status == "success":
                # Only mark the day successful if it still is: a downgraded
                # run exits 1, alerts, and is recorded as partial, so leaving
                # a success marker behind would contradict every other signal.
                write_today_success_marker(
                    log_dir, today_local, marker_namespace="local"
                )
                write_today_success_marker(log_dir, today_utc, marker_namespace="utc")

        # Anything short of a clean success asks for a retry too: that covers
        # a run downgraded to partial after the pipeline returned (the Azure
        # checkpoint was not written).
        result.retry_wanted = result.retry_wanted or not (
            result.ok and result.status == "success"
        )

        result.history_recorded = append_run_history(
            str(log_dir),
            {
                "kind": kind,
                "attempt_id": attempt_id,
                "retry_wanted": result.retry_wanted,
                "status": result.status,
                "from_date": str(result.from_date),
                "to_date": str(result.to_date),
                "from_dt_utc": from_dt_utc.isoformat(),
                "to_dt_utc": to_dt_utc.isoformat(),
                "rows_downloaded": result.rows_downloaded,
                "rows_in_canonical": result.rows_in_canonical,
                "azure_published": result.azure_published,
                "gap_reason": gap_reason,
                "message": result.message,
                "run_started": run_started.isoformat(),
                "run_finished": run_finished.isoformat(),
                "schedule_timezone": schedule_cfg.timezone,
                "today_local": today_local.isoformat(),
                "today_utc": today_utc.isoformat(),
                "last_confirmed_settled_at_utc": (
                    result.last_confirmed_settled_at_utc.isoformat()
                    if result.last_confirmed_settled_at_utc is not None
                    else None
                ),
            },
        )
        return result

    def stood_down() -> Optional[RunResult]:
        """A ``superseded`` result when this is a retry that may not proceed."""
        if claim is None or claim():
            return None
        logger.info("The retry is no longer due; standing down.")
        return RunResult(
            ok=True,
            status="success",
            superseded=True,
            message="Retry stood down: no longer due.",
        )

    try:
        results_dir = _resolve_results_dir(creds)
    except Exception:
        # A malformed paths section. There is no directory to lock; the
        # pipeline reports the configuration error as a failed run, as it
        # did before the lock existed. A retry still claims its attempt
        # first: every retry that proceeds must already be counted.
        return stood_down() or attempt(lock_held=False)

    try:
        with pipeline_lock(
            results_dir,
            command=command,
            wait_seconds=0.0 if is_retry else SCHEDULED_LOCK_WAIT_SECONDS,
        ):
            return stood_down() or attempt(lock_held=True)
    except PipelineBusy as exc:
        logger.error("%s", exc)
        busy = RunResult(
            ok=False,
            status="failed",
            message=str(exc),
            retry_wanted=True,
            busy=True,
        )
        if is_retry:
            logger.info("Another downloader command is running; the retry stands down.")
            return busy
        # A scheduled run that never got the lock is a failed attempt.
        return record_refusal(busy)


def run_backfill(
    creds: dict[str, Any],
    schedule_cfg: ScheduleConfig,
    from_date: date,
    to_date: date,
) -> RunResult:
    if from_date > to_date:
        msg = f"Invalid backfill range: from_date ({from_date}) > to_date ({to_date})."
        logger.error(msg)
        return RunResult(
            ok=False, status="failed", from_date=from_date, to_date=to_date, message=msg
        )

    log_dir = _resolve_log_dir(creds, schedule_cfg)
    run_started = datetime.now(timezone.utc)

    # A manual backfill rewrites the same canonical as a scheduled run; the
    # hard floor applies identically, against the resolved output directory
    # (an empty results_csv_dir falls back to the OneDrive default, which can
    # sit on a different filesystem from the working directory).
    disk_ok, disk_warning = _preflight_disk(creds)
    if not disk_ok:
        logger.error(disk_warning)
        result = RunResult(
            ok=False,
            status="failed",
            from_date=from_date,
            to_date=to_date,
            message=disk_warning,
        )
        append_run_history(
            str(log_dir),
            {
                "status": result.status,
                "kind": "backfill",
                "mode": "backfill",
                "from_date": str(from_date),
                "to_date": str(to_date),
                "message": result.message,
                "run_started": run_started.isoformat(),
                "run_finished": datetime.now(timezone.utc).isoformat(),
            },
        )
        return result

    from_dt_utc = datetime(
        from_date.year, from_date.month, from_date.day, 0, 0, 0, tzinfo=timezone.utc
    )
    # Exclusive upper bound at midnight after to_date, so the final day is
    # fully covered with no sub-second blind spot before midnight.
    end_day = to_date + timedelta(days=1)
    to_dt_utc = datetime(end_day.year, end_day.month, end_day.day, tzinfo=timezone.utc)

    logger.info("Backfill run: %s → %s", from_dt_utc, to_dt_utc)
    # No wait: a person typed this and should be told at once what is running.
    result = _run_pipeline(
        creds, schedule_cfg, from_dt_utc, to_dt_utc, command="backfill"
    )
    if disk_warning:
        # The operator sees only the result message; a soft warning discarded
        # here would leave a manual rewrite of the canonical unwarned.
        result.message = f"{result.message} {disk_warning}".strip()
    result.from_date = from_date
    result.to_date = to_date
    result.from_dt_utc = from_dt_utc
    result.to_dt_utc = to_dt_utc

    run_finished = datetime.now(timezone.utc)

    append_run_history(
        str(log_dir),
        {
            "mode": "backfill",
            "status": result.status,
            "from_date": str(from_date),
            "to_date": str(to_date),
            "from_dt_utc": from_dt_utc.isoformat(),
            "to_dt_utc": to_dt_utc.isoformat(),
            "rows_downloaded": result.rows_downloaded,
            "rows_in_canonical": result.rows_in_canonical,
            "azure_published": result.azure_published,
            "message": result.message,
            "run_started": run_started.isoformat(),
            "run_finished": run_finished.isoformat(),
            "schedule_timezone": schedule_cfg.timezone,
            "last_confirmed_settled_at_utc": (
                result.last_confirmed_settled_at_utc.isoformat()
                if result.last_confirmed_settled_at_utc is not None
                else None
            ),
        },
    )

    return result

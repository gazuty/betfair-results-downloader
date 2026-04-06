"""
scheduler/runner.py
~~~~~~~~~~~~~~~~~~~
Headless scheduled download runner (Phase 2.2).

Public entry points:

- :func:`run_scheduled` — called by the ``run`` CLI subcommand.  Checks
  today's success marker, computes the backfill window via gap detection,
  runs the pipeline, and updates state on success.

- :func:`run_backfill` — called by the ``backfill`` CLI subcommand.  Same
  pipeline with an explicit date range override; no skip-marker check.

Both functions respect the four-gate Azure publish matrix::

    user.enable_azure_sql AND NOT user.dry_run
    AND schedule.publish_to_azure AND schedule.allow_azure_publish

If any gate is closed, CSV outputs are written but Azure publishing is
skipped silently.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Optional

from ..config import ScheduleConfig
from ..paths import get_results_database_dir
from .auth import build_api_client
from .gap_detector import compute_backfill_window
from .state import (
    append_run_history,
    check_today_success_marker,
    read_schedule_state,
    upsert_schedule_state,
    write_today_success_marker,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Result container
# ---------------------------------------------------------------------------

@dataclass
class RunResult:
    """Summary returned by :func:`run_scheduled` and :func:`run_backfill`."""

    ok: bool
    skipped: bool = False
    skip_reason: str = ""
    status: str = ""          # "success", "partial", "failed", "skipped"
    from_date: Optional[date] = None
    to_date: Optional[date] = None
    rows_downloaded: int = 0
    rows_in_canonical: int = 0
    azure_published: bool = False
    message: str = ""
    errors: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Azure publish gate
# ---------------------------------------------------------------------------

def _azure_publish_allowed(creds: dict[str, Any], schedule_cfg: ScheduleConfig) -> bool:
    """
    Return True only when all four gates are open:

    1. user.enable_azure_sql = true
    2. user.dry_run = false
    3. schedule.publish_to_azure = true
    4. schedule.allow_azure_publish = true
    """
    user = creds.get("user") or {}
    enable_azure = bool(user.get("enable_azure_sql", False))
    dry_run = bool(user.get("dry_run", True))
    return (
        enable_azure
        and not dry_run
        and schedule_cfg.publish_to_azure
        and schedule_cfg.allow_azure_publish
    )


# ---------------------------------------------------------------------------
# Core pipeline
# ---------------------------------------------------------------------------

def _resolve_repo_root() -> Path:
    """Return the repo root relative to this file's package location."""
    return Path(__file__).resolve().parents[3]


def _resolve_results_dir(creds: dict[str, Any]) -> Path:
    raw = (creds.get("paths") or {}).get("results_csv_dir", "")
    return Path(raw) if raw else get_results_database_dir()


def _resolve_log_dir(creds: dict[str, Any], schedule_cfg: ScheduleConfig) -> Path:
    """Resolve the log directory: schedule_cfg.log_dir → repo/outputs."""
    if schedule_cfg.log_dir:
        return Path(schedule_cfg.log_dir).expanduser()
    repo_root = _resolve_repo_root()
    return repo_root / "outputs"


def _run_pipeline(
    creds: dict[str, Any],
    schedule_cfg: ScheduleConfig,
    from_date: date,
    to_date: date,
) -> RunResult:
    """
    Execute the four-phase pipeline (fetch → enrich → CSV → Azure) for an
    explicit date range.  Called by both :func:`run_scheduled` and
    :func:`run_backfill`.

    Calls Betfair API using cert-based auth (owned internally; logout in
    ``finally``).
    """
    results_dir = _resolve_results_dir(creds)

    betfair_creds = creds.get("betfair") or {}
    repo_root = _resolve_repo_root()

    run_started = datetime.now(timezone.utc)
    client = None

    try:
        # --- Auth ---
        logger.info("Authenticating with Betfair (cert-based)...")
        try:
            client = build_api_client(betfair_creds)
        except Exception as exc:
            msg = f"Betfair authentication failed: {exc}"
            logger.error(msg)
            return RunResult(ok=False, status="failed", from_date=from_date,
                             to_date=to_date, message=msg)

        # --- Phase 1: Download ---
        from ..downloader_core import fetch_cleared_orders_df_range  # noqa: PLC0415
        logger.info("Downloading cleared orders %s → %s (chunk_days=%d)...",
                    from_date, to_date, schedule_cfg.chunk_days)

        def _say(msg: str) -> None:
            logger.info(msg)

        dl = fetch_cleared_orders_df_range(
            betfair=betfair_creds,
            from_date=from_date,
            to_date=to_date,
            chunk_days=schedule_cfg.chunk_days,
            api_client=client,
            status_cb=_say,
        )
        logger.info("Download result: %s", dl.message)

        if not dl.attempted or dl.df_co is None or dl.df_co.empty:
            return RunResult(
                ok=True,
                status="success",
                from_date=from_date,
                to_date=to_date,
                rows_downloaded=0,
                message=f"Download returned no rows. {dl.message}",
            )

        df_co = dl.df_co

        # --- Phase 2: Enrich ---
        from ..downloader_core import enrich_with_market_catalogue  # noqa: PLC0415
        logger.info("Enriching with market catalogue...")
        df_co, enr = enrich_with_market_catalogue(
            df_co=df_co,
            betfair=betfair_creds,
            repo_root=repo_root,
            enable=True,
            use_cache=True,
            api_client=client,
            status_cb=_say,
        )
        logger.info("Enrich result: %s", enr.message)

        # --- Phase 3: CSV ---
        from ..downloader_core import write_csv_outputs  # noqa: PLC0415
        logger.info("Writing CSV outputs to %s...", results_dir)
        csvr = write_csv_outputs(df_co=df_co, results_csv_dir=results_dir, status_cb=_say)
        logger.info("CSV result: %s", csvr.message)

        # --- Phase 4: Azure (conditional) ---
        azure_published = False
        azure_message = ""
        if _azure_publish_allowed(creds, schedule_cfg):
            logger.info("Azure publish gates open — publishing...")
            try:
                from ..downloader_core import prepare_azure_dataset  # noqa: PLC0415
                from ..azure_publish import publish_to_azure_sql  # noqa: PLC0415
                prep = prepare_azure_dataset(df_co=df_co, allowed_event_type_ids={7, 4339})
                if prep.attempted and prep.rows_to_write:
                    az = publish_to_azure_sql(
                        creds=creds,
                        rows_to_write=prep.rows_to_write,
                        dry_run=False,
                    )
                    azure_published = az.attempted
                    azure_message = az.message
                    logger.info("Azure publish result: %s", az.message)
                else:
                    azure_message = prep.message
                    logger.info("Azure prep result: %s (nothing to write)", prep.message)
            except Exception as exc:
                azure_message = f"Azure publish failed: {exc}"
                logger.warning(azure_message)
                # Partial success: CSV written but Azure failed — report but don't fail the run
                return RunResult(
                    ok=True,
                    status="partial",
                    from_date=from_date,
                    to_date=to_date,
                    rows_downloaded=dl.rows_downloaded,
                    rows_in_canonical=csvr.rows_in_canonical,
                    azure_published=False,
                    message=(
                        f"CSV written ({csvr.rows_in_canonical:,} rows canonical). "
                        f"Azure publish failed: {exc}"
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
            azure_message = f"Azure publish skipped ({', '.join(gates) or 'gates closed'})."
            logger.info(azure_message)

        run_finished = datetime.now(timezone.utc)
        elapsed = (run_finished - run_started).total_seconds()
        summary = (
            f"Scheduled run complete: {dl.rows_downloaded:,} rows downloaded, "
            f"{csvr.rows_in_canonical:,} rows in canonical, "
            f"azure={'published' if azure_published else 'skipped'}. "
            f"Elapsed {elapsed:.1f}s."
        )
        logger.info(summary)

        return RunResult(
            ok=True,
            status="success",
            from_date=from_date,
            to_date=to_date,
            rows_downloaded=dl.rows_downloaded,
            rows_in_canonical=csvr.rows_in_canonical,
            azure_published=azure_published,
            message=summary,
        )

    finally:
        if client is not None:
            try:
                client.logout()
                logger.debug("Betfair logout clean.")
            except Exception as exc:
                logger.warning("Betfair logout raised: %s", exc)


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------

def run_scheduled(
    creds: dict[str, Any],
    schedule_cfg: ScheduleConfig,
) -> RunResult:
    """
    Execute one scheduled download run for the current day.

    1. Check today's success marker — skip silently if already covered.
    2. Authenticate via cert-based Betfair login.
    3. Compute the backfill window via :func:`gap_detector.compute_backfill_window`.
    4. Call the pipeline (fetch → enrich → CSV → Azure).
    5. On success: upsert ScheduleState, write success marker, append run_history.
    6. On partial/failure: append run_history, do NOT advance LastCoveredDateUtc.

    Parameters
    ----------
    creds:
        Full credentials dict (as returned by :func:`secrets.load_credentials`).
    schedule_cfg:
        Parsed :class:`ScheduleConfig`.

    Returns
    -------
    RunResult
        Summary of the run.  ``ok=True, skipped=True`` if already covered.
    """
    today = datetime.now(timezone.utc).date()
    log_dir = _resolve_log_dir(creds, schedule_cfg)
    run_started = datetime.now(timezone.utc)

    # --- Skip check ---
    if check_today_success_marker(log_dir, today):
        msg = f"Today ({today}) already covered — skipping."
        logger.info(msg)
        return RunResult(ok=True, skipped=True, skip_reason=msg, status="skipped", message=msg)

    # --- Gap detection ---
    logger.info("Computing backfill window...")
    from_date, to_date, gap_reason = compute_backfill_window(creds, schedule_cfg)
    logger.info("Backfill window: %s → %s (%s)", from_date, to_date, gap_reason)

    # --- Run pipeline ---
    result = _run_pipeline(creds, schedule_cfg, from_date, to_date)
    result.from_date = from_date
    result.to_date = to_date

    run_finished = datetime.now(timezone.utc)

    # --- Update state on success ---
    if result.ok and result.status == "success":
        upsert_schedule_state(
            creds,
            last_covered_date=to_date,
            status="success",
            message=result.message,
            run_started_utc=run_started,
            run_finished_utc=run_finished,
        )
        write_today_success_marker(log_dir, today)

    # --- Record history always ---
    append_run_history(str(log_dir), {
        "status": result.status,
        "from_date": str(from_date),
        "to_date": str(to_date),
        "rows_downloaded": result.rows_downloaded,
        "rows_in_canonical": result.rows_in_canonical,
        "azure_published": result.azure_published,
        "gap_reason": gap_reason,
        "message": result.message,
        "run_started": run_started.isoformat(),
        "run_finished": run_finished.isoformat(),
    })

    return result


def run_backfill(
    creds: dict[str, Any],
    schedule_cfg: ScheduleConfig,
    from_date: date,
    to_date: date,
) -> RunResult:
    """
    Run the pipeline for an explicit date range (manual backfill).

    No skip-marker check.  Respects the same four-gate Azure publish matrix
    as :func:`run_scheduled`.  Does NOT update ``LastCoveredDateUtc`` or
    write a success marker (backfills are ad-hoc, not authoritative state).

    Parameters
    ----------
    creds:
        Full credentials dict.
    schedule_cfg:
        Parsed :class:`ScheduleConfig`.
    from_date:
        Inclusive start date (UTC calendar date).
    to_date:
        Inclusive end date (UTC calendar date).

    Returns
    -------
    RunResult
        Summary of the backfill run.
    """
    if from_date > to_date:
        msg = f"Invalid backfill range: from_date ({from_date}) > to_date ({to_date})."
        logger.error(msg)
        return RunResult(ok=False, status="failed", from_date=from_date,
                         to_date=to_date, message=msg)

    log_dir = _resolve_log_dir(creds, schedule_cfg)
    run_started = datetime.now(timezone.utc)

    logger.info("Backfill run: %s → %s", from_date, to_date)
    result = _run_pipeline(creds, schedule_cfg, from_date, to_date)
    result.from_date = from_date
    result.to_date = to_date

    run_finished = datetime.now(timezone.utc)

    append_run_history(str(log_dir), {
        "mode": "backfill",
        "status": result.status,
        "from_date": str(from_date),
        "to_date": str(to_date),
        "rows_downloaded": result.rows_downloaded,
        "rows_in_canonical": result.rows_in_canonical,
        "azure_published": result.azure_published,
        "message": result.message,
        "run_started": run_started.isoformat(),
        "run_finished": run_finished.isoformat(),
    })

    return result

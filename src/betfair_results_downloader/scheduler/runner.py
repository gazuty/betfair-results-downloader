"""
scheduler/runner.py
~~~~~~~~~~~~~~~~~~~
Headless scheduled download runner (Phase 2.2).

Public entry points:

- :func:`run_scheduled` — called by the ``run`` CLI subcommand. Checks
  scheduler-local success markers, computes the backfill window via gap detection,
  runs the pipeline, and updates state on success.

- :func:`run_backfill` — called by the ``backfill`` CLI subcommand. Same
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
from ..paths import resolve_results_dir
from .auth import build_api_client
from .gap_detector import compute_backfill_window
from .state import (
    append_run_history,
    check_today_success_marker,
    upsert_schedule_state,
    write_today_success_marker,
)
from .time_semantics import get_scheduler_now

logger = logging.getLogger(__name__)


@dataclass
class RunResult:
    """Summary returned by :func:`run_scheduled` and :func:`run_backfill`."""

    ok: bool
    skipped: bool = False
    skip_reason: str = ""
    status: str = ""
    from_date: Optional[date] = None
    to_date: Optional[date] = None
    rows_downloaded: int = 0
    rows_in_canonical: int = 0
    azure_published: bool = False
    message: str = ""
    errors: list[str] = field(default_factory=list)


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


def _resolve_log_dir(creds: dict[str, Any], schedule_cfg: ScheduleConfig) -> Path:
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
    results_dir = _resolve_results_dir(creds)
    betfair_creds = creds.get("betfair") or {}
    run_started = datetime.now(timezone.utc)
    client = None

    try:
        logger.info("Authenticating with Betfair (cert-based)...")
        try:
            client = build_api_client(betfair_creds)
        except Exception as exc:
            msg = f"Betfair authentication failed: {exc}"
            logger.error(msg)
            return RunResult(ok=False, status="failed", from_date=from_date,
                             to_date=to_date, message=msg)

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

        from ..downloader_core import enrich_with_market_catalogue, resolve_enrichment_cache_dir  # noqa: PLC0415
        logger.info("Enriching with market catalogue...")
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

        from ..downloader_core import write_csv_outputs  # noqa: PLC0415
        logger.info("Writing CSV outputs to %s...", results_dir)
        csvr = write_csv_outputs(df_co=df_co, results_csv_dir=results_dir, status_cb=_say)
        logger.info("CSV result: %s", csvr.message)

        azure_published = False
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
                    logger.info("Azure publish result: %s", az.message)
                else:
                    logger.info("Azure prep result: %s (nothing to write)", prep.message)
            except Exception as exc:
                logger.warning("Azure publish failed: %s", exc)
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
            logger.info("Azure publish skipped (%s).", ", ".join(gates) or "gates closed")

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


def run_scheduled(
    creds: dict[str, Any],
    schedule_cfg: ScheduleConfig,
) -> RunResult:
    scheduler_now = get_scheduler_now(schedule_cfg)
    today_local = scheduler_now.today_local
    today_utc = scheduler_now.today_utc
    log_dir = _resolve_log_dir(creds, schedule_cfg)
    run_started = scheduler_now.now_utc

    if check_today_success_marker(log_dir, today_local, marker_namespace="local"):
        msg = (
            f"Today ({today_local}) already covered in {schedule_cfg.timezone} "
            f"— skipping. UTC day={today_utc}."
        )
        logger.info(msg)
        return RunResult(ok=True, skipped=True, skip_reason=msg, status="skipped", message=msg)

    logger.info("Computing backfill window...")
    from_date, to_date, gap_reason = compute_backfill_window(creds, schedule_cfg)
    logger.info("Backfill window: %s → %s (%s)", from_date, to_date, gap_reason)

    result = _run_pipeline(creds, schedule_cfg, from_date, to_date)
    result.from_date = from_date
    result.to_date = to_date

    run_finished = datetime.now(timezone.utc)

    if result.ok and result.status == "success":
        upsert_schedule_state(
            creds,
            last_covered_date_utc=today_utc,
            last_covered_date_local=today_local,
            last_covered_timezone=schedule_cfg.timezone,
            status="success",
            message=result.message,
            run_started_utc=run_started,
            run_finished_utc=run_finished,
        )
        write_today_success_marker(log_dir, today_local, marker_namespace="local")
        write_today_success_marker(log_dir, today_utc, marker_namespace="utc")

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
        "schedule_timezone": schedule_cfg.timezone,
        "today_local": today_local.isoformat(),
        "today_utc": today_utc.isoformat(),
    })

    return result


def run_backfill(
    creds: dict[str, Any],
    schedule_cfg: ScheduleConfig,
    from_date: date,
    to_date: date,
) -> RunResult:
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
        "schedule_timezone": schedule_cfg.timezone,
    })

    return result

"""
scheduler/runner.py
~~~~~~~~~~~~~~~~~~~
Headless scheduled download runner.

Scheduled runs now use timestamp-based incremental checkpoints so all four
configured daily run times can perform real download attempts.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

import pandas as pd

from ..config import ScheduleConfig
from ..paths import resolve_results_dir
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


def _resolve_log_dir(creds: dict[str, Any], schedule_cfg: ScheduleConfig) -> Path:
    if schedule_cfg.log_dir:
        return Path(schedule_cfg.log_dir).expanduser()
    repo_root = _resolve_repo_root()
    return repo_root / "outputs"


def _extract_max_settled_at_utc(df: pd.DataFrame) -> Optional[datetime]:
    if df is None or df.empty or "settledDate" not in df.columns:
        return None
    ts = pd.to_datetime(df["settledDate"], utc=True, errors="coerce").dropna()
    if ts.empty:
        return None
    max_ts = ts.max().to_pydatetime()
    if max_ts.tzinfo is None:
        max_ts = max_ts.replace(tzinfo=timezone.utc)
    return max_ts.astimezone(timezone.utc)


def _run_pipeline(
    creds: dict[str, Any],
    schedule_cfg: ScheduleConfig,
    from_dt_utc: datetime,
    to_dt_utc: datetime,
) -> RunResult:
    """
    Run the download → enrich → CSV → Azure pipeline for one window.

    Never raises: any unhandled exception is converted into a failed
    :class:`RunResult` so the caller always records the attempt in
    ``run_history.jsonl``.
    """
    try:
        return _run_pipeline_inner(creds, schedule_cfg, from_dt_utc, to_dt_utc)
    except Exception as exc:
        logger.exception("Scheduled pipeline failed with an unhandled error.")
        return RunResult(
            ok=False,
            status="failed",
            from_dt_utc=from_dt_utc,
            to_dt_utc=to_dt_utc,
            message=f"Scheduled pipeline failed: {type(exc).__name__}: {exc}",
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
            msg = f"Betfair authentication failed: {exc}"
            logger.error(msg)
            return RunResult(
                ok=False,
                status="failed",
                from_dt_utc=from_dt_utc,
                to_dt_utc=to_dt_utc,
                message=msg,
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
            return RunResult(
                ok=True,
                status="success",
                rows_downloaded=0,
                from_dt_utc=from_dt_utc,
                to_dt_utc=to_dt_utc,
                last_confirmed_settled_at_utc=None,
                download_started_utc=run_started,
                download_finished_utc=datetime.now(timezone.utc),
                message=f"Download returned no rows. {dl.message}",
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

        max_settled_at_utc = _extract_max_settled_at_utc(df_co)

        azure_published = False
        if _azure_publish_allowed(creds, schedule_cfg):
            logger.info("Azure publish gates open, publishing...")
            try:
                from ..downloader_core import prepare_azure_dataset  # noqa: PLC0415
                from ..azure_publish import publish_to_azure_sql  # noqa: PLC0415

                prep = prepare_azure_dataset(df_co=df_co)
                if prep.attempted and prep.rows_to_write:
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
            logger.info(
                "Azure publish skipped (%s).", ", ".join(gates) or "gates closed"
            )

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
            rows_downloaded=dl.rows_downloaded,
            rows_in_canonical=csvr.rows_in_canonical,
            azure_published=azure_published,
            message=summary,
            from_dt_utc=from_dt_utc,
            to_dt_utc=to_dt_utc,
            last_confirmed_settled_at_utc=max_settled_at_utc,
            download_started_utc=run_started,
            download_finished_utc=run_finished,
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

    logger.info("Computing incremental backfill window...")
    from_dt_utc, to_dt_utc, gap_reason = compute_backfill_window(creds, schedule_cfg)
    logger.info("Backfill window: %s → %s (%s)", from_dt_utc, to_dt_utc, gap_reason)

    result = _run_pipeline(creds, schedule_cfg, from_dt_utc, to_dt_utc)
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
            # Only mark the day successful if it still is: a downgraded run
            # exits 1, alerts, and is recorded as partial, so leaving a success
            # marker behind would contradict every other signal.
            write_today_success_marker(log_dir, today_local, marker_namespace="local")
            write_today_success_marker(log_dir, today_utc, marker_namespace="utc")

    append_run_history(
        str(log_dir),
        {
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

    from_dt_utc = datetime(
        from_date.year, from_date.month, from_date.day, 0, 0, 0, tzinfo=timezone.utc
    )
    # Exclusive upper bound at midnight after to_date, so the final day is
    # fully covered with no sub-second blind spot before midnight.
    end_day = to_date + timedelta(days=1)
    to_dt_utc = datetime(end_day.year, end_day.month, end_day.day, tzinfo=timezone.utc)

    logger.info("Backfill run: %s → %s", from_dt_utc, to_dt_utc)
    result = _run_pipeline(creds, schedule_cfg, from_dt_utc, to_dt_utc)
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

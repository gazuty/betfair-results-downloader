from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path
from typing import Callable, Optional, Tuple

import pandas as pd

from .audit import compute_missing_settled_dates
from .state import load_run_state

FALLBACK_DAYS = 90
FALLBACK_NOTE = (
    "No existing data - Recommend 90 days capture, however this may take some time."
)


def _emit(status_cb: Optional[Callable[[str], None]], msg: str) -> None:
    if not status_cb:
        return
    try:
        status_cb(msg)
    except Exception:
        pass


def compute_days_to_download(
    last_settled_dt: datetime | date,
    now_utc: datetime,
    *,
    min_days: int = 1,
    max_days: int = 90,
) -> int:
    if isinstance(last_settled_dt, datetime):
        if last_settled_dt.tzinfo is None:
            last_settled_dt = last_settled_dt.replace(tzinfo=timezone.utc)
        last_date_utc = last_settled_dt.astimezone(timezone.utc).date()
    else:
        last_date_utc = last_settled_dt

    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)
    today_utc = now_utc.astimezone(timezone.utc).date()

    gap_days = max((today_utc - last_date_utc).days, 0)
    return min(max_days, max(min_days, gap_days + 1))


def _parse_iso_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    raw = value.strip()
    if not raw:
        return None
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(raw)
    except Exception:
        return None


def recommend_lookback_days(
    results_csv_dir: Path,
    canonical_filename: str = "cleared_orders_cleaned.csv",
    status_cb: Optional[Callable[[str], None]] = None,
    now_utc: Optional[datetime] = None,
) -> Tuple[int, str, Optional[date], str]:
    now_utc = now_utc or datetime.now(timezone.utc)

    run_state_path = results_csv_dir / "run_state.json"
    run_state = load_run_state(run_state_path)
    if run_state:
        last_success_raw = str(run_state.get("last_success_utc") or "").strip()
        last_success_dt = _parse_iso_datetime(last_success_raw)
        if last_success_dt is not None:
            if last_success_dt.tzinfo is None:
                last_success_dt = last_success_dt.replace(tzinfo=timezone.utc)
            recommended_days = compute_days_to_download(last_success_dt, now_utc)
            last_success_utc = last_success_dt.astimezone(timezone.utc).date()
            _emit(
                status_cb, f"Lookback: lookback_source=run_state path={run_state_path}"
            )
            _emit(
                status_cb,
                f"Lookback: last_success_utc={last_success_dt} tzinfo={last_success_dt.tzinfo}",
            )
            _emit(status_cb, f"Lookback: now_utc={now_utc} tzinfo={now_utc.tzinfo}")
            _emit(
                status_cb,
                f"Lookback: safety_overlap=1, recommended_days={recommended_days}",
            )
            note = (
                f"Run state found (last_success_utc: {last_success_utc:%Y-%m-%d}) - "
                f"Recommend capture {recommended_days} days (includes 1-day overlap)."
            )
            return recommended_days, note, last_success_utc, "run_state"

    canonical_path = results_csv_dir / canonical_filename
    if not canonical_path.exists():
        _emit(
            status_cb,
            f"Lookback: canonical CSV missing at {canonical_path}. Using fallback {FALLBACK_DAYS} days.",
        )
        _emit(status_cb, "Lookback: lookback_source=csv_heuristic")
        return FALLBACK_DAYS, FALLBACK_NOTE, None, "csv_heuristic"

    try:
        df = pd.read_csv(canonical_path, usecols=["settledDate"], low_memory=False)
    except Exception:
        _emit(
            status_cb,
            f"Lookback: failed to read {canonical_path}. Using fallback {FALLBACK_DAYS} days.",
        )
        _emit(status_cb, "Lookback: lookback_source=csv_heuristic")
        return FALLBACK_DAYS, FALLBACK_NOTE, None, "csv_heuristic"

    if "settledDate" not in df.columns or df.empty:
        _emit(
            status_cb,
            f"Lookback: no settledDate rows in {canonical_path}. Using fallback {FALLBACK_DAYS} days.",
        )
        _emit(status_cb, "Lookback: lookback_source=csv_heuristic")
        return FALLBACK_DAYS, FALLBACK_NOTE, None, "csv_heuristic"

    ts = pd.to_datetime(df["settledDate"], utc=True, errors="coerce").dropna()
    if ts.empty:
        _emit(
            status_cb,
            f"Lookback: settledDate parse failed for {canonical_path}. Using fallback {FALLBACK_DAYS} days.",
        )
        _emit(status_cb, "Lookback: lookback_source=csv_heuristic")
        return FALLBACK_DAYS, FALLBACK_NOTE, None, "csv_heuristic"

    last_settled_ts = ts.max()
    last_settled_dt = last_settled_ts.to_pydatetime()
    last_settled_utc_dt = last_settled_dt.astimezone(timezone.utc)
    last_settled_utc = last_settled_utc_dt.date()
    now_utc = now_utc or datetime.now(timezone.utc)
    gap_days = max((now_utc.date() - last_settled_utc).days, 0)
    recommended_days = compute_days_to_download(last_settled_utc_dt, now_utc)

    _emit(status_cb, "Lookback: lookback_source=csv_heuristic")
    _emit(status_cb, f"Lookback: canonical_csv={canonical_path}")
    _emit(status_cb, f"Lookback: rows={len(df):,}, settledDate_valid={len(ts):,}")
    _emit(
        status_cb,
        f"Lookback: last_settled_ts={last_settled_dt} tzinfo={last_settled_dt.tzinfo}",
    )
    _emit(status_cb, f"Lookback: now_utc={now_utc} tzinfo={now_utc.tzinfo}")
    _emit(
        status_cb,
        f"Lookback: gap_days={gap_days}, safety_overlap=1, recommended_days={recommended_days}",
    )

    note = (
        f"Existing data found (latest settledDate: {last_settled_utc:%Y-%m-%d} UTC, heuristic) - "
        f"Recommend capture {recommended_days} days (includes 1-day overlap)."
    )
    return recommended_days, note, last_settled_utc, "csv_heuristic"


def _clamp_days(value: int, *, min_days: int = 1, max_days: int = 90) -> int:
    return min(max_days, max(min_days, value))


def recommend_lookback_days_v2(
    canonical_csv_path: Path,
    run_state_path: Path,
    *,
    status_cb: Optional[Callable[[str], None]] = None,
    now_utc: Optional[datetime] = None,
    window_days: int = 90,
) -> dict[str, object]:
    now_utc = now_utc or datetime.now(timezone.utc)
    today_utc = now_utc.date()

    audit = compute_missing_settled_dates(
        canonical_csv_path,
        window_days=window_days,
        now_utc=now_utc,
    )

    missing_ranges = audit.get("missing_ranges") or []
    window_start = audit.get("window_start")
    window_end = audit.get("window_end")

    if missing_ranges:
        most_recent = missing_ranges[-1]
        earliest_missing_date = date.fromisoformat(str(most_recent["start"]))
        recommended_days = _clamp_days((today_utc - earliest_missing_date).days + 2)
        note = (
            "Missing settledDate range detected (backfillable). "
            f"Missing range {most_recent['start']}..{most_recent['end']}; "
            f"window {window_start}..{window_end}; "
            f"Betfair max backfill {window_days} days."
        )
        _emit(status_cb, "Lookback v2: lookback_source=missing_dates")
        _emit(
            status_cb,
            f"Lookback v2: missing_range={most_recent['start']}..{most_recent['end']}",
        )
        _emit(status_cb, f"Lookback v2: window={window_start}..{window_end}")
        _emit(
            status_cb,
            f"Lookback v2: today_utc={today_utc}, recommended_days={recommended_days}",
        )
        return {
            "recommended_days": recommended_days,
            "lookback_source": "missing_dates",
            "recommendation_note": note,
            "today_utc": today_utc.isoformat(),
            "window_start": window_start,
            "window_end": window_end,
            "missing_range": most_recent,
        }

    run_state = load_run_state(run_state_path)
    if run_state:
        last_success_raw = str(run_state.get("last_success_utc") or "").strip()
        last_success_dt = _parse_iso_datetime(last_success_raw)
        if last_success_dt is not None:
            if last_success_dt.tzinfo is None:
                last_success_dt = last_success_dt.replace(tzinfo=timezone.utc)
            recommended_days = compute_days_to_download(last_success_dt, now_utc)
            last_success_utc = last_success_dt.astimezone(timezone.utc).date()
            note = (
                f"Run state found (last_success_utc: {last_success_utc:%Y-%m-%d}) - "
                f"Recommend capture {recommended_days} days (includes 1-day overlap)."
            )
            _emit(status_cb, "Lookback v2: lookback_source=run_state")
            _emit(
                status_cb,
                f"Lookback v2: last_success_utc={last_success_dt} tzinfo={last_success_dt.tzinfo}",
            )
            _emit(
                status_cb,
                f"Lookback v2: today_utc={today_utc}, recommended_days={recommended_days}",
            )
            return {
                "recommended_days": recommended_days,
                "lookback_source": "run_state",
                "recommendation_note": note,
                "today_utc": today_utc.isoformat(),
                "last_success_utc": last_success_utc.isoformat(),
                "missing_range": None,
            }

    latest_present = audit.get("latest")
    if latest_present:
        latest_date = date.fromisoformat(str(latest_present))
        recommended_days = compute_days_to_download(latest_date, now_utc)
        note = (
            f"Existing data found (latest settledDate: {latest_date:%Y-%m-%d} UTC) - "
            f"Recommend capture {recommended_days} days (includes 1-day overlap)."
        )
        _emit(status_cb, "Lookback v2: lookback_source=csv_latest")
        _emit(status_cb, f"Lookback v2: latest_settled_utc={latest_date}")
        _emit(
            status_cb,
            f"Lookback v2: today_utc={today_utc}, recommended_days={recommended_days}",
        )
        return {
            "recommended_days": recommended_days,
            "lookback_source": "csv_latest",
            "recommendation_note": note,
            "today_utc": today_utc.isoformat(),
            "latest_settled_utc": latest_date.isoformat(),
            "missing_range": None,
        }

    _emit(status_cb, "Lookback v2: lookback_source=first_run_default")
    return {
        "recommended_days": FALLBACK_DAYS,
        "lookback_source": "first_run_default",
        "recommendation_note": "No prior data found; using maximum backfill window (90 days).",
        "today_utc": today_utc.isoformat(),
        "missing_range": None,
    }

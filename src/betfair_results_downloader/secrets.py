from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


def repo_root() -> Path:
    # src/betfair_results_downloader/secrets.py -> repo root
    return Path(__file__).resolve().parents[2]


def secrets_dir() -> Path:
    return repo_root() / "secrets"


def credentials_pointer_path() -> Path:
    """
    Stores the user-selected credentials path (if any), so we can load/save credentials.json
    from a custom location while keeping a stable repo default.
    """
    return secrets_dir() / "credentials.location.json"


def _normalize_path(p: str) -> Path:
    path = Path(p).expanduser()
    # If user gives a relative path, treat it as relative to repo root
    if not path.is_absolute():
        path = (repo_root() / path).resolve()
    return path.resolve()


def get_credentials_path() -> Path:
    """
    Resolve the credentials path from the pointer file if it exists and is valid.
    Otherwise, return the repo default secrets/credentials.json.
    """
    default = secrets_dir() / "credentials.json"
    ptr = credentials_pointer_path()

    if not ptr.exists():
        return default

    try:
        # utf-8-sig safely handles UTF-8 BOM if present (Windows editors sometimes add this)
        data = json.loads(ptr.read_text(encoding="utf-8-sig"))
        raw = str(data.get("path", "")).strip()
        if not raw:
            return default
        return _normalize_path(raw)
    except Exception:
        return default


def set_credentials_path(path: Path) -> None:
    """
    Persist a user-selected credentials path to the pointer file.
    """
    resolved = path.expanduser()
    if not resolved.is_absolute():
        resolved = (repo_root() / resolved).resolve()

    credentials_pointer_path().parent.mkdir(parents=True, exist_ok=True)
    credentials_pointer_path().write_text(
        json.dumps({"path": str(resolved)}, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def credentials_path() -> Path:
    """
    Backwards-compatible accessor used throughout the codebase.
    """
    return get_credentials_path()


def credentials_template_path() -> Path:
    return secrets_dir() / "credentials.template.json"


def _read_json(path: Path) -> dict[str, Any]:
    """
    Read JSON with BOM tolerance.

    Windows tools (including some editors) may save JSON with a UTF-8 BOM.
    json.loads() will error if we read that with plain "utf-8".
    "utf-8-sig" strips BOM if present and behaves like UTF-8 otherwise.
    """
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def get_nested(dct: dict[str, Any], dotted_key: str, default: Any = None) -> Any:
    cur: Any = dct
    for part in dotted_key.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur


def set_nested(dct: dict[str, Any], dotted_key: str, value: Any) -> None:
    cur: dict[str, Any] = dct
    parts = dotted_key.split(".")
    for p in parts[:-1]:
        if p not in cur or not isinstance(cur[p], dict):
            cur[p] = {}
        cur = cur[p]
    cur[parts[-1]] = value


def mask_value(value: str | None) -> str:
    if not value:
        return ""
    if len(value) <= 4:
        return "*" * len(value)
    return value[:2] + "*" * (len(value) - 4) + value[-2:]


@dataclass
class CredentialValidation:
    ok: bool
    errors: list[str]
    warnings: list[str] = field(default_factory=list)


_HH_MM_RE = re.compile(r"^\d{2}:\d{2}$")


def _validate_schedule_section(
    creds: dict[str, Any],
) -> tuple[list[str], list[str]]:
    """
    Validate the ``schedule`` block of credentials when ``schedule.enabled`` is true.

    Returns ``(errors, warnings)`` — errors are blocking; warnings are informational.
    Returns two empty lists when ``schedule.enabled`` is false (no-op).
    """
    from .config import parse_schedule_config

    schedule_cfg = parse_schedule_config(creds)
    if not schedule_cfg.enabled:
        return [], []

    errors: list[str] = []
    warnings: list[str] = []

    # --- betfair.certs_dir ---
    certs_dir_raw = (get_nested(creds, "betfair.certs_dir") or "").strip()
    if not certs_dir_raw:
        errors.append("schedule.enabled=true requires betfair.certs_dir to be set")
    else:
        certs_path = Path(certs_dir_raw).expanduser()
        if not certs_path.is_dir():
            errors.append(
                f"betfair.certs_dir does not exist or is not a directory: {certs_path}"
            )
        else:
            from .scheduler.auth import CERT_FILENAME, KEY_FILENAME
            missing = [
                name for name, p in [
                    (CERT_FILENAME, certs_path / CERT_FILENAME),
                    (KEY_FILENAME, certs_path / KEY_FILENAME),
                ]
                if not p.exists()
            ]
            if missing:
                errors.append(
                    f"betfair.certs_dir is missing cert file(s): {', '.join(missing)}"
                )

    # --- timezone ---
    try:
        from zoneinfo import ZoneInfo
        ZoneInfo(schedule_cfg.timezone)
    except Exception:
        errors.append(
            f"schedule.timezone is not a valid IANA timezone: {schedule_cfg.timezone!r}"
        )

    # --- time formats ---
    if not _HH_MM_RE.match(schedule_cfg.primary_time):
        errors.append(
            f"schedule.primary_time must be HH:MM, got: {schedule_cfg.primary_time!r}"
        )
    for t in schedule_cfg.retry_times:
        if not _HH_MM_RE.match(str(t)):
            errors.append(
                f"schedule.retry_times entry must be HH:MM, got: {t!r}"
            )

    # --- numeric bounds ---
    if schedule_cfg.max_backfill_days > 365:
        errors.append(
            f"schedule.max_backfill_days must be <= 365, got: {schedule_cfg.max_backfill_days}"
        )
    if schedule_cfg.chunk_days > 90:
        errors.append(
            f"schedule.chunk_days must be <= 90, got: {schedule_cfg.chunk_days}"
        )

    # --- Azure publish warnings (not errors) ---
    if schedule_cfg.allow_azure_publish:
        enable_azure = bool(get_nested(creds, "user.enable_azure_sql", False))
        dry_run = bool(get_nested(creds, "user.dry_run", True))
        if not enable_azure:
            warnings.append(
                "schedule.allow_azure_publish=true but user.enable_azure_sql=false "
                "— Azure publish will be skipped"
            )
        if dry_run:
            warnings.append(
                "schedule.allow_azure_publish=true but user.dry_run=true "
                "— Azure publish will be skipped"
            )

    return errors, warnings


def validate_credentials(creds: dict[str, Any]) -> CredentialValidation:
    """
    Validate a credentials dict, collecting all errors before returning.

    Includes schedule-section validation when ``schedule.enabled`` is true;
    skips it entirely (zero behaviour change) when ``schedule.enabled`` is false.
    """
    errors: list[str] = []
    warnings: list[str] = []

    # Betfair required
    if not get_nested(creds, "betfair.username"):
        errors.append("Missing betfair.username")
    if not get_nested(creds, "betfair.password"):
        errors.append("Missing betfair.password")
    if not get_nested(creds, "betfair.app_key"):
        errors.append("Missing betfair.app_key")

    # Azure: only required if enable_azure_sql is true
    enable_azure = bool(get_nested(creds, "user.enable_azure_sql", False))
    if enable_azure:
        if not get_nested(creds, "azure_sql.server"):
            errors.append("Missing azure_sql.server (Azure enabled)")
        if not get_nested(creds, "azure_sql.database"):
            errors.append("Missing azure_sql.database (Azure enabled)")
        if not get_nested(creds, "azure_sql.username"):
            errors.append("Missing azure_sql.username (Azure enabled)")
        if not get_nested(creds, "azure_sql.password"):
            errors.append("Missing azure_sql.password (Azure enabled)")
        # driver is optional; we'll provide a default

    # Schedule validation (no-op when schedule.enabled=false)
    sched_errors, sched_warnings = _validate_schedule_section(creds)
    errors.extend(sched_errors)
    warnings.extend(sched_warnings)

    return CredentialValidation(ok=(len(errors) == 0), errors=errors, warnings=warnings)


def default_credentials_structure() -> dict[str, Any]:
    """
    Canonical fallback structure used when no template exists.
    """
    return {
        "betfair": {"username": "", "password": "", "app_key": ""},
        "user": {
            "user_id": "YourUserName",
            "days": 7,
            "include_horses": True,
            "include_greyhounds": True,
            "enable_azure_sql": False,
            "dry_run": True,
        },
        "paths": {
            "results_csv_dir": "",
        },
        "azure_sql": {
            "server": "",
            "database": "",
            "username": "",
            "password": "",
            "driver": "ODBC Driver 18 for SQL Server",
        },
        "google_sheets": {
            "sheet_name": "",
            "service_account_path": "",
        },
    }


def load_credentials_template() -> dict[str, Any]:
    """
    Load credentials template if present; otherwise return fallback default.
    """
    templ = _read_json(credentials_template_path())
    if templ:
        # Tidy up any historical hardcoded username, if present in template
        uid = str(get_nested(templ, "user.user_id", "")).strip().lower()
        if uid == "gazuty":
            set_nested(templ, "user.user_id", "YourUserName")
        return templ

    return default_credentials_structure()


def load_credentials(path: Path | None = None) -> dict[str, Any]:
    p = path or credentials_path()
    return _read_json(p)


def save_credentials(creds: dict[str, Any], path: Path | None = None) -> None:
    p = path or credentials_path()
    _write_json(p, creds)


def ensure_credentials_file_exists() -> None:
    """
    If credentials file doesn't exist at the resolved credentials_path(),
    create it from template (if present), otherwise create fallback default.
    """
    path = credentials_path()
    if path.exists():
        return

    templ = load_credentials_template()
    _write_json(path, templ)

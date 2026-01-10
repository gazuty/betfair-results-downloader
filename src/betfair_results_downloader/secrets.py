from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


def repo_root() -> Path:
    # src/betfair_results_downloader/secrets.py -> repo root
    return Path(__file__).resolve().parents[2]


def secrets_dir() -> Path:
    return repo_root() / "secrets"


def credentials_path() -> Path:
    return secrets_dir() / "credentials.json"


def credentials_template_path() -> Path:
    return secrets_dir() / "credentials.template.json"


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


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


def validate_credentials(creds: dict[str, Any]) -> CredentialValidation:
    errors: list[str] = []

    # Betfair required
    if not get_nested(creds, "betfair.username"):
        errors.append("Missing betfair.username")
    if not get_nested(creds, "betfair.password"):
        errors.append("Missing betfair.password")
    if not get_nested(creds, "betfair.app_key"):
        errors.append("Missing betfair.app_key")

    # Optional user id (but recommended)
    # Not required for local runs; Azure tends to need it
    # if not get_nested(creds, "user.user_id"):
    #     errors.append("Missing user.user_id")

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

    return CredentialValidation(ok=(len(errors) == 0), errors=errors)


def load_credentials() -> dict[str, Any]:
    return _read_json(credentials_path())


def save_credentials(creds: dict[str, Any]) -> None:
    _write_json(credentials_path(), creds)


def ensure_credentials_file_exists() -> None:
    """
    If secrets/credentials.json doesn't exist, create it from template (if present),
    otherwise create a sensible default structure.
    """
    path = credentials_path()
    if path.exists():
        return

    templ = _read_json(credentials_template_path())
    if templ:
        _write_json(path, templ)
        return

    # fallback default
    default = {
        "betfair": {"username": "", "password": "", "app_key": ""},
        "user": {
            "user_id": "Gazuty",
            "days": 7,
            "include_horses": True,
            "include_greyhounds": True,
            "enable_azure_sql": False,
            "dry_run": True,
        },
        "azure_sql": {
            "server": "",
            "database": "",
            "username": "",
            "password": "",
            "driver": "ODBC Driver 18 for SQL Server",
        },
    }
    _write_json(path, default)

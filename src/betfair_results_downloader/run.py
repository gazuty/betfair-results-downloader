from __future__ import annotations

from dataclasses import asdict
from typing import Any

from .config import DownloaderConfig
from .secrets import load_credentials, validate_credentials, get_nested


def run_downloader(config: DownloaderConfig, creds: dict[str, Any]) -> dict[str, Any]:
    """
    Official programmatic entrypoint for GUI/CLI.

    For now: validate inputs + return a structured "plan".
    Next step: wire this into your existing downloader core (move logic out of notebook).
    """
    config.validate()

    v = validate_credentials(creds)
    if not v.ok:
        raise ValueError("Invalid credentials:\n- " + "\n- ".join(v.errors))

    plan = {
        "days": config.days,
        "event_type_ids": config.selected_event_type_ids(),
        "include_horses": config.include_horses,
        "include_greyhounds": config.include_greyhounds,
        "enable_azure_sql": config.enable_azure_sql,
        "dry_run": config.dry_run,
        "user_id": config.user_id,
    }

    # Placeholder until core pipeline is wired:
    # return results summary structure; GUI will display this.
    return {
        "ok": True,
        "message": "Validated config + credentials. Core download pipeline not yet wired on feature/gui.",
        "plan": plan,
    }


def _config_from_creds(creds: dict[str, Any]) -> DownloaderConfig:
    user_id = get_nested(creds, "user.user_id", None)
    days = int(get_nested(creds, "user.days", 7))
    include_horses = bool(get_nested(creds, "user.include_horses", True))
    include_greyhounds = bool(get_nested(creds, "user.include_greyhounds", True))
    enable_azure_sql = bool(get_nested(creds, "user.enable_azure_sql", False))
    dry_run = bool(get_nested(creds, "user.dry_run", True))

    return DownloaderConfig(
        days=days,
        include_horses=include_horses,
        include_greyhounds=include_greyhounds,
        enable_azure_sql=enable_azure_sql,
        dry_run=dry_run,
        user_id=user_id,
    )


def main() -> None:
    creds = load_credentials()
    cfg = _config_from_creds(creds)
    result = run_downloader(cfg, creds)
    print(result["message"])
    print("Plan:", result["plan"])


if __name__ == "__main__":
    main()

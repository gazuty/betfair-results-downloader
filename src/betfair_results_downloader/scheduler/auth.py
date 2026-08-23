from __future__ import annotations

from pathlib import Path
from typing import Any

import betfairlightweight


CERT_FILENAME = "client-2048.crt"
KEY_FILENAME = "client-2048.key"


def build_api_client(betfair_creds: dict[str, Any]) -> betfairlightweight.APIClient:
    """
    Build and log in a Betfair APIClient using cert-based non-interactive auth.

    Required fields in ``betfair_creds``:
        - ``username``
        - ``password``
        - ``app_key``
        - ``certs_dir`` — directory containing ``client-2048.crt`` and ``client-2048.key``
          (both files must exist and the directory must be readable)

    Returns a client with a valid ``session_token``. The caller owns the client
    and is responsible for calling ``client.logout()`` when finished.

    Raises ``RuntimeError`` with a precise, actionable message on any
    configuration problem; the underlying ``betfairlightweight`` exception is
    propagated on network/auth failure.
    """
    username = (betfair_creds.get("username") or "").strip()
    password = betfair_creds.get("password") or ""
    app_key = (betfair_creds.get("app_key") or "").strip()
    certs_dir_raw = (betfair_creds.get("certs_dir") or "").strip()

    if not username:
        raise RuntimeError(
            "Betfair cert login requires betfair.username in credentials."
        )
    if not password:
        raise RuntimeError(
            "Betfair cert login requires betfair.password in credentials."
        )
    if not app_key:
        raise RuntimeError(
            "Betfair cert login requires betfair.app_key in credentials."
        )
    if not certs_dir_raw:
        raise RuntimeError(
            "Betfair cert login requires betfair.certs_dir in credentials.json "
            f"(a directory containing {CERT_FILENAME} and {KEY_FILENAME})."
        )

    certs_dir = Path(certs_dir_raw).expanduser()
    if not certs_dir.is_dir():
        raise RuntimeError(
            f"betfair.certs_dir does not exist or is not a directory: {certs_dir}"
        )

    crt_path = certs_dir / CERT_FILENAME
    key_path = certs_dir / KEY_FILENAME
    missing = [p.name for p in (crt_path, key_path) if not p.exists()]
    if missing:
        raise RuntimeError(
            f"Cert pair incomplete in {certs_dir}. Missing: {', '.join(missing)}. "
            f"Expected {CERT_FILENAME} and {KEY_FILENAME}."
        )

    client = betfairlightweight.APIClient(
        username=username,
        password=password,
        app_key=app_key,
        certs=str(certs_dir),
    )
    client.login()
    return client

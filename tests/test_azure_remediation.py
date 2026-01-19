from betfair_results_downloader import azure_remediation


def test_get_scoped_user_id_prefers_db_user_id(monkeypatch) -> None:
    def _fake_load() -> dict:
        return {"user": {"db_user_id": "DbUser", "user_id": "Fallback"}}

    monkeypatch.setattr(azure_remediation, "_load_creds", _fake_load)

    assert azure_remediation.get_scoped_user_id() == "DbUser"


def test_get_scoped_user_id_falls_back_to_user_id(monkeypatch) -> None:
    def _fake_load() -> dict:
        return {"user": {"user_id": "UserOnly"}}

    monkeypatch.setattr(azure_remediation, "_load_creds", _fake_load)

    assert azure_remediation.get_scoped_user_id() == "UserOnly"

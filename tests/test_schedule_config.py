"""Tests for Phase 1.2: ScheduleConfig dataclass and schedule validation."""

from __future__ import annotations

import pytest

from betfair_results_downloader.config import parse_schedule_config
from betfair_results_downloader.secrets import (
    validate_credentials,
    _validate_schedule_section,
)


# ---------------------------------------------------------------------------
# parse_schedule_config
# ---------------------------------------------------------------------------


class TestParseScheduleConfig:
    def test_empty_creds_returns_disabled_defaults(self) -> None:
        cfg = parse_schedule_config({})
        assert cfg.enabled is False
        assert cfg.timezone == "Australia/Sydney"
        assert cfg.primary_time == "06:00"
        assert cfg.retry_times == ("09:00", "19:00", "23:00")
        assert cfg.publish_to_azure is True
        assert cfg.allow_azure_publish is False
        assert cfg.max_backfill_days == 90
        assert cfg.chunk_days == 30
        assert cfg.min_overlap_hours == 2
        assert cfg.log_dir == ""

    def test_absent_schedule_key_returns_defaults(self) -> None:
        cfg = parse_schedule_config({"betfair": {"username": "x"}})
        assert cfg.enabled is False

    def test_none_schedule_value_returns_defaults(self) -> None:
        cfg = parse_schedule_config({"schedule": None})
        assert cfg.enabled is False

    def test_full_schedule_block_parsed(self) -> None:
        creds = {
            "schedule": {
                "enabled": True,
                "timezone": "Europe/London",
                "primary_time": "07:30",
                "retry_times": ["10:00", "20:00"],
                "publish_to_azure": False,
                "allow_azure_publish": True,
                "max_backfill_days": 30,
                "chunk_days": 14,
                "min_overlap_hours": 4,
                "log_dir": "/tmp/logs",
            }
        }
        cfg = parse_schedule_config(creds)
        assert cfg.enabled is True
        assert cfg.timezone == "Europe/London"
        assert cfg.primary_time == "07:30"
        assert cfg.retry_times == ("10:00", "20:00")
        assert cfg.publish_to_azure is False
        assert cfg.allow_azure_publish is True
        assert cfg.max_backfill_days == 30
        assert cfg.chunk_days == 14
        assert cfg.min_overlap_hours == 4
        assert cfg.log_dir == "/tmp/logs"

    def test_partial_schedule_block_fills_defaults(self) -> None:
        cfg = parse_schedule_config({"schedule": {"enabled": True}})
        assert cfg.enabled is True
        assert cfg.timezone == "Australia/Sydney"
        assert cfg.primary_time == "06:00"

    def test_empty_retry_times_produces_empty_tuple(self) -> None:
        cfg = parse_schedule_config({"schedule": {"retry_times": []}})
        assert cfg.retry_times == ()

    def test_schedule_config_is_frozen(self) -> None:
        cfg = parse_schedule_config({})
        with pytest.raises(Exception):
            cfg.enabled = True  # type: ignore[misc]


# ---------------------------------------------------------------------------
# _validate_schedule_section (internal)
# ---------------------------------------------------------------------------


def _base_valid_creds(certs_dir: str) -> dict:
    """Minimal valid credentials with schedule enabled."""
    return {
        "betfair": {
            "username": "u",
            "password": "p",
            "app_key": "k",
            "certs_dir": certs_dir,
        },
        "user": {"enable_azure_sql": False, "dry_run": True},
        "schedule": {
            "enabled": True,
            "timezone": "Australia/Sydney",
            "primary_time": "06:00",
            "retry_times": ["09:00"],
            "max_backfill_days": 90,
            "chunk_days": 30,
        },
    }


class TestValidateScheduleSection:
    def test_disabled_returns_no_errors(self) -> None:
        errors, warnings = _validate_schedule_section({"schedule": {"enabled": False}})
        assert errors == []
        assert warnings == []

    def test_missing_schedule_key_returns_no_errors(self) -> None:
        errors, warnings = _validate_schedule_section({})
        assert errors == []

    def test_missing_certs_dir_is_an_error(self, tmp_path) -> None:
        creds = {
            "betfair": {"certs_dir": ""},
            "schedule": {
                "enabled": True,
                "timezone": "Australia/Sydney",
                "primary_time": "06:00",
                "retry_times": [],
            },
        }
        errors, _ = _validate_schedule_section(creds)
        assert any("certs_dir" in e for e in errors)

    def test_nonexistent_certs_dir_is_an_error(self) -> None:
        creds = {
            "betfair": {"certs_dir": "/nonexistent/path/does/not/exist"},
            "schedule": {
                "enabled": True,
                "timezone": "Australia/Sydney",
                "primary_time": "06:00",
                "retry_times": [],
            },
        }
        errors, _ = _validate_schedule_section(creds)
        assert any("certs_dir" in e for e in errors)

    def test_certs_dir_missing_key_file_is_error(self, tmp_path) -> None:
        # Directory exists but only has .crt, not .key
        (tmp_path / "client-2048.crt").write_text("fake cert")
        creds = {
            "betfair": {"certs_dir": str(tmp_path)},
            "schedule": {
                "enabled": True,
                "timezone": "Australia/Sydney",
                "primary_time": "06:00",
                "retry_times": [],
            },
        }
        errors, _ = _validate_schedule_section(creds)
        assert any("client-2048.key" in e for e in errors)

    def test_invalid_timezone_is_error(self, tmp_path) -> None:
        (tmp_path / "client-2048.crt").write_text("x")
        (tmp_path / "client-2048.key").write_text("x")
        creds = {
            "betfair": {"certs_dir": str(tmp_path)},
            "schedule": {
                "enabled": True,
                "timezone": "NotATimezone/Fake",
                "primary_time": "06:00",
                "retry_times": [],
            },
        }
        errors, _ = _validate_schedule_section(creds)
        assert any("timezone" in e for e in errors)

    def test_invalid_primary_time_format_is_error(self, tmp_path) -> None:
        (tmp_path / "client-2048.crt").write_text("x")
        (tmp_path / "client-2048.key").write_text("x")
        creds = {
            "betfair": {"certs_dir": str(tmp_path)},
            "schedule": {
                "enabled": True,
                "timezone": "Australia/Sydney",
                "primary_time": "6:00",  # missing leading zero
                "retry_times": [],
            },
        }
        errors, _ = _validate_schedule_section(creds)
        assert any("primary_time" in e for e in errors)

    def test_invalid_retry_time_format_is_error(self, tmp_path) -> None:
        (tmp_path / "client-2048.crt").write_text("x")
        (tmp_path / "client-2048.key").write_text("x")
        creds = {
            "betfair": {"certs_dir": str(tmp_path)},
            "schedule": {
                "enabled": True,
                "timezone": "Australia/Sydney",
                "primary_time": "06:00",
                "retry_times": ["9:00", "19:00"],
            },  # bad first entry
        }
        errors, _ = _validate_schedule_section(creds)
        assert any("retry_times" in e for e in errors)

    def test_max_backfill_days_over_365_is_error(self, tmp_path) -> None:
        (tmp_path / "client-2048.crt").write_text("x")
        (tmp_path / "client-2048.key").write_text("x")
        creds = {
            "betfair": {"certs_dir": str(tmp_path)},
            "schedule": {
                "enabled": True,
                "timezone": "Australia/Sydney",
                "primary_time": "06:00",
                "retry_times": [],
                "max_backfill_days": 366,
            },
        }
        errors, _ = _validate_schedule_section(creds)
        assert any("max_backfill_days" in e for e in errors)

    def test_allow_azure_publish_without_enable_azure_sql_is_warning(
        self, tmp_path
    ) -> None:
        (tmp_path / "client-2048.crt").write_text("x")
        (tmp_path / "client-2048.key").write_text("x")
        creds = {
            "betfair": {"certs_dir": str(tmp_path)},
            "user": {"enable_azure_sql": False, "dry_run": False},
            "schedule": {
                "enabled": True,
                "timezone": "Australia/Sydney",
                "primary_time": "06:00",
                "retry_times": [],
                "allow_azure_publish": True,
            },
        }
        errors, warnings = _validate_schedule_section(creds)
        assert not any("enable_azure_sql" in e for e in errors), (
            "should be warning, not error"
        )
        assert any("enable_azure_sql" in w for w in warnings)

    def test_allow_azure_publish_with_dry_run_is_warning(self, tmp_path) -> None:
        (tmp_path / "client-2048.crt").write_text("x")
        (tmp_path / "client-2048.key").write_text("x")
        creds = {
            "betfair": {"certs_dir": str(tmp_path)},
            "user": {"enable_azure_sql": True, "dry_run": True},
            "schedule": {
                "enabled": True,
                "timezone": "Australia/Sydney",
                "primary_time": "06:00",
                "retry_times": [],
                "allow_azure_publish": True,
            },
        }
        errors, warnings = _validate_schedule_section(creds)
        assert not any("dry_run" in e for e in errors), "should be warning, not error"
        assert any("dry_run" in w for w in warnings)

    def test_all_errors_collected_together(self, tmp_path) -> None:
        """Multiple errors should all be returned, not fail-fast."""
        creds = {
            "betfair": {"certs_dir": ""},
            "schedule": {
                "enabled": True,
                "timezone": "BadZone",
                "primary_time": "6:0",
                "retry_times": ["bad"],
                "max_backfill_days": 400,
                "chunk_days": 100,
            },
        }
        errors, _ = _validate_schedule_section(creds)
        assert (
            len(errors) >= 5
        )  # certs_dir, timezone, primary_time, retry, max_backfill, chunk


# ---------------------------------------------------------------------------
# validate_credentials (public API) — schedule integration
# ---------------------------------------------------------------------------


class TestValidateCredentialsScheduleIntegration:
    def test_schedule_disabled_skips_schedule_validation(self) -> None:
        creds = {
            "betfair": {"username": "u", "password": "p", "app_key": "k"},
            "schedule": {"enabled": False, "timezone": "NotATimezone"},
        }
        result = validate_credentials(creds)
        # Bad timezone should NOT surface since enabled=False
        assert not any("timezone" in e for e in result.errors)
        assert not any("timezone" in e for e in result.warnings)

    def test_validate_credentials_result_has_warnings_field(self) -> None:
        creds = {"betfair": {"username": "u", "password": "p", "app_key": "k"}}
        result = validate_credentials(creds)
        assert hasattr(result, "warnings")
        assert isinstance(result.warnings, list)

    def test_schedule_errors_surface_in_validate_credentials(self) -> None:
        creds = {
            "betfair": {"username": "u", "password": "p", "app_key": "k"},
            "schedule": {"enabled": True, "certs_dir": ""},
        }
        result = validate_credentials(creds)
        assert not result.ok
        assert any("certs_dir" in e for e in result.errors)


class TestResultsDirValidation:
    """paths.results_csv_dir is required since H4 removed the fallback."""

    def _creds(self, paths) -> dict:
        return {
            "betfair": {"username": "u", "password": "p", "app_key": "k"},
            "user": {"enable_azure_sql": False},
            "paths": paths,
        }

    def test_missing_results_dir_is_an_error(self) -> None:
        result = validate_credentials(self._creds({}))
        assert not result.ok
        assert any("results_csv_dir" in e for e in result.errors)

    def test_absent_paths_section_is_an_error(self) -> None:
        creds = self._creds({})
        del creds["paths"]
        result = validate_credentials(creds)
        assert not result.ok
        assert any("results_csv_dir" in e for e in result.errors)

    def test_non_object_paths_is_an_error(self) -> None:
        result = validate_credentials(self._creds("not-an-object"))
        assert not result.ok
        assert any("paths must be an object" in e for e in result.errors)

    def test_set_results_dir_is_valid(self) -> None:
        result = validate_credentials(self._creds({"results_csv_dir": "~/BetfairData"}))
        assert result.ok, result.errors

    def test_non_string_results_dir_is_an_error(self) -> None:
        """str() coercion would resolve `true` to a directory named True."""
        result = validate_credentials(self._creds({"results_csv_dir": True}))
        assert not result.ok
        assert any("must be a string" in e for e in result.errors)

    def test_non_string_backup_dir_is_an_error(self) -> None:
        result = validate_credentials(
            self._creds({"results_csv_dir": "~/BetfairData", "backup_dir": 123})
        )
        assert not result.ok
        assert any("backup_dir must be a string" in e for e in result.errors)

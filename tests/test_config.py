from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from pr_html_preview.config import AppConfig, normalize_source_dir, parse_bool, validate_identifier

REQUIRED = {
    "GITHUB_APP_ID": "123",
    "GITHUB_PRIVATE_KEY": "test-key",
    "GITHUB_WEBHOOK_SECRET": "webhook-secret",
    "PREVIEW_BASE_URL": "https://preview.example.com",
    "STORAGE_ROOT": "/tmp/pr-html-preview-tests",
    "PREVIEW_USERNAME": "preview",
    "PREVIEW_PASSWORD": "generated-test-password",
}


def set_required(monkeypatch: pytest.MonkeyPatch) -> None:
    for key, value in REQUIRED.items():
        monkeypatch.setenv(key, value)


def test_load_from_env_parses_required_and_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    set_required(monkeypatch)

    monkeypatch.setenv("PREVIEW_MAX_FILE_COUNT", "12")
    monkeypatch.setenv("PREVIEW_SOURCE_DIR_OVERRIDES", json.dumps({"octo-org/example-repo": "site/output"}))
    config = AppConfig.load_from_env()
    assert config.github_app_id == 123

    assert config.max_file_count == 12
    assert config.source_dir_for("octo-org", "example-repo") == "site/output"
    assert config.source_dir_for("octo-org", "other-repo") == "docs/html"


def test_load_from_env_reports_missing_variable(monkeypatch: pytest.MonkeyPatch) -> None:
    set_required(monkeypatch)
    monkeypatch.delenv("GITHUB_APP_ID")
    with pytest.raises(ValueError, match="GITHUB_APP_ID"):
        AppConfig.load_from_env()


@pytest.mark.parametrize("value,expected", [("true", True), ("OFF", False), (None, True)])
def test_parse_bool(value: str | None, expected: bool) -> None:
    assert parse_bool(value, True) is expected


def test_parse_bool_rejects_unknown_value() -> None:
    with pytest.raises(ValueError):
        parse_bool("sometimes", False)


@pytest.mark.parametrize("value", ["", "/docs", "docs/", "docs\\html", "docs/../private", " docs"])
def test_source_directory_rejects_unsafe_values(value: str) -> None:
    with pytest.raises(ValueError):
        normalize_source_dir(value)


def test_config_rejects_insecure_or_non_origin_base_url(app_config: AppConfig) -> None:
    data = app_config.model_dump()
    data["preview_base_url"] = "http://preview.example.com/path"
    with pytest.raises(ValidationError):
        AppConfig(**data)


def test_config_allows_http_only_when_explicit_for_local_development(app_config: AppConfig) -> None:
    data = app_config.model_dump()
    data.update(preview_base_url="http://localhost:8080", allow_insecure_preview_url=True)
    assert AppConfig(**data).preview_base_url == "http://localhost:8080"


@pytest.mark.parametrize("field", ["request_body_max_bytes", "max_file_count", "max_file_size_bytes", "max_total_bytes"])
def test_config_rejects_non_positive_limits(app_config: AppConfig, field: str) -> None:
    data = app_config.model_dump()
    data[field] = 0
    with pytest.raises(ValidationError):
        AppConfig(**data)


def test_identifier_validation() -> None:
    assert validate_identifier("octo-org") == "octo-org"
    with pytest.raises(ValueError):
        validate_identifier("../owner")


def test_load_from_env_rejects_bad_limit_override_and_repository_map(monkeypatch: pytest.MonkeyPatch) -> None:
    set_required(monkeypatch)
    monkeypatch.setenv("PREVIEW_MAX_FILE_COUNT", "0")
    with pytest.raises(ValueError, match="positive"):
        AppConfig.load_from_env()
    monkeypatch.setenv("PREVIEW_MAX_FILE_COUNT", "2")
    monkeypatch.setenv("PREVIEW_SOURCE_DIR_OVERRIDES", "[]")
    with pytest.raises(ValueError, match="JSON object"):
        AppConfig.load_from_env()
    monkeypatch.setenv("PREVIEW_SOURCE_DIR_OVERRIDES", '{"bad-key":"docs"}')
    with pytest.raises(ValueError, match="owner/repo"):
        AppConfig.load_from_env()


def test_storage_root_must_be_directory(app_config: AppConfig, tmp_path) -> None:
    file_path = tmp_path / "file"
    file_path.write_text("x")
    data = app_config.model_dump()
    data["storage_root"] = file_path
    with pytest.raises(ValidationError, match="directory"):
        AppConfig(**data)


def test_secret_values_are_redacted_and_file_credentials_rotate(app_config: AppConfig, tmp_path) -> None:
    username_file = tmp_path / "username"
    password_file = tmp_path / "password"
    username_file.write_text("preview")
    password_file.write_text("first-password")
    data = app_config.model_dump()
    data.update(
        preview_password=None,
        preview_username_file=username_file,
        preview_password_file=password_file,
    )
    config = AppConfig(**data)
    assert "test-key" not in repr(config)
    assert "webhook-secret" not in repr(config)
    assert config.preview_credentials() == ("preview", "first-password")
    password_file.write_text("rotated-password")
    assert config.preview_credentials() == ("preview", "rotated-password")

    unavailable = app_config.model_copy(update={"preview_password": None, "preview_password_file": None})
    with pytest.raises(ValueError, match="unavailable"):
        unavailable.preview_credentials()

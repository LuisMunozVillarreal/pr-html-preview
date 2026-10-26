from __future__ import annotations

import json
import os
import re
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

IDENTIFIER_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9._-]{0,98}[A-Za-z0-9_-])?")


def parse_bool(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"invalid boolean value: {value!r}")


def normalize_source_dir(value: str) -> str:
    if not value or value != value.strip() or value.endswith("/") or "\\" in value or any(ord(char) < 32 for char in value):
        raise ValueError("source directory must be a non-empty normalized relative POSIX path")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError("source directory must be a normalized relative POSIX path")
    normalized = path.as_posix()
    if normalized.endswith("/"):
        raise ValueError("source directory cannot end with a slash")
    return normalized


def validate_identifier(value: str, field_name: str = "identifier") -> str:
    if not IDENTIFIER_RE.fullmatch(value):
        raise ValueError(f"invalid {field_name}")
    return value


def _required_env(name: str) -> str:
    value = os.getenv(name)
    if value is None or value == "":
        raise ValueError(f"missing required environment variable: {name}")
    return value


def _positive_env(name: str, default: int) -> int:
    value = int(os.getenv(name, str(default)))
    if value <= 0:
        raise ValueError(f"{name} must be positive")
    return value


def _parse_overrides(raw: str | None) -> dict[str, str]:
    if not raw:
        return {}
    value: Any = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("PREVIEW_SOURCE_DIR_OVERRIDES must be a JSON object")
    result: dict[str, str] = {}
    for repository, source_dir in value.items():
        if not isinstance(repository, str) or not isinstance(source_dir, str) or repository.count("/") != 1:
            raise ValueError("source overrides must map owner/repo strings to directory strings")
        owner, repo = repository.split("/", 1)
        validate_identifier(owner, "owner")
        validate_identifier(repo, "repository")
        result[repository] = normalize_source_dir(source_dir)
    return result


class AppConfig(BaseModel):
    model_config = ConfigDict(hide_input_in_errors=True)

    github_app_id: int
    github_private_key: SecretStr
    github_webhook_secret: SecretStr
    preview_base_url: str
    storage_root: Path
    preview_username: str
    preview_password: SecretStr | None = None
    preview_username_file: Path | None = None
    preview_password_file: Path | None = None
    request_body_max_bytes: int = 1024 * 1024
    max_file_count: int = 2000
    max_file_size_bytes: int = 2 * 1024 * 1024
    max_total_bytes: int = 150 * 1024 * 1024
    max_storage_bytes: int = 750 * 1024 * 1024
    max_preview_count: int = 100
    min_free_bytes: int = 100 * 1024 * 1024
    default_source_dir: str = "docs/html"
    source_dir_overrides: dict[str, str] = Field(default_factory=dict)
    allow_insecure_preview_url: bool = False

    @classmethod
    def load_from_env(cls) -> AppConfig:
        return cls(
            github_app_id=int(_required_env("GITHUB_APP_ID")),
            github_private_key=SecretStr(_required_env("GITHUB_PRIVATE_KEY").replace("\\n", "\n")),
            github_webhook_secret=SecretStr(_required_env("GITHUB_WEBHOOK_SECRET")),
            preview_base_url=_required_env("PREVIEW_BASE_URL"),
            storage_root=Path(_required_env("STORAGE_ROOT")),
            preview_username=os.getenv("PREVIEW_USERNAME", "preview"),
            preview_password=SecretStr(value) if (value := os.getenv("PREVIEW_PASSWORD")) else None,
            preview_username_file=Path(value) if (value := os.getenv("PREVIEW_USERNAME_FILE")) else None,
            preview_password_file=Path(value) if (value := os.getenv("PREVIEW_PASSWORD_FILE")) else None,
            request_body_max_bytes=_positive_env("GITHUB_WEBHOOK_BODY_MAX_BYTES", 1024 * 1024),
            max_file_count=_positive_env("PREVIEW_MAX_FILE_COUNT", 2000),
            max_file_size_bytes=_positive_env("PREVIEW_MAX_FILE_SIZE_BYTES", 2 * 1024 * 1024),
            max_total_bytes=_positive_env("PREVIEW_MAX_TOTAL_BYTES", 150 * 1024 * 1024),
            max_storage_bytes=_positive_env("PREVIEW_MAX_STORAGE_BYTES", 750 * 1024 * 1024),
            max_preview_count=_positive_env("PREVIEW_MAX_PREVIEW_COUNT", 100),
            min_free_bytes=_positive_env("PREVIEW_MIN_FREE_BYTES", 100 * 1024 * 1024),
            default_source_dir=normalize_source_dir(os.getenv("PREVIEW_DEFAULT_SOURCE_DIR", "docs/html")),
            source_dir_overrides=_parse_overrides(os.getenv("PREVIEW_SOURCE_DIR_OVERRIDES")),
            allow_insecure_preview_url=parse_bool(os.getenv("PREVIEW_ALLOW_HTTP"), False),
        )

    @field_validator("github_app_id")
    @classmethod
    def validate_app_id(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("GITHUB_APP_ID must be positive")
        return value

    @field_validator("github_private_key", "github_webhook_secret")
    @classmethod
    def validate_secret_text(cls, value: SecretStr) -> SecretStr:
        raw = value.get_secret_value()
        if not raw or "\x00" in raw or "\r" in raw:
            raise ValueError("credential values must be non-empty and contain no NUL or carriage return")
        return value

    @field_validator(
        "request_body_max_bytes",
        "max_file_count",
        "max_file_size_bytes",
        "max_total_bytes",
        "max_storage_bytes",
        "max_preview_count",
        "min_free_bytes",
    )
    @classmethod
    def validate_positive(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("limits must be positive")
        return value

    @field_validator("default_source_dir")
    @classmethod
    def validate_default_source_dir(cls, value: str) -> str:
        return normalize_source_dir(value)

    @model_validator(mode="after")
    def validate_model(self) -> AppConfig:
        parsed = urlparse(self.preview_base_url)
        if parsed.scheme not in ({"http", "https"} if self.allow_insecure_preview_url else {"https"}):
            raise ValueError("PREVIEW_BASE_URL must use HTTPS unless PREVIEW_ALLOW_HTTP=true")
        if not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in {"", "/"}:
            raise ValueError("PREVIEW_BASE_URL must be an origin without credentials, path, query, or fragment")
        self.preview_base_url = self.preview_base_url.rstrip("/")
        self.storage_root = self.storage_root.expanduser().resolve()
        if self.storage_root.exists() and not self.storage_root.is_dir():
            raise ValueError("STORAGE_ROOT must be a directory")
        for repository, source_dir in self.source_dir_overrides.items():
            owner, repo = repository.split("/", 1)
            validate_identifier(owner, "owner")
            validate_identifier(repo, "repository")
            normalize_source_dir(source_dir)
        if self.preview_password is None and self.preview_password_file is None:
            raise ValueError("PREVIEW_PASSWORD or PREVIEW_PASSWORD_FILE is required")
        if self.preview_password_file is not None:
            self.preview_password_file = self.preview_password_file.resolve()
        if self.preview_username_file is not None:
            self.preview_username_file = self.preview_username_file.resolve()
        return self

    def ensure_storage_root(self) -> Path:
        self.storage_root.mkdir(parents=True, exist_ok=True)
        return self.storage_root

    def source_dir_for(self, owner: str, repo: str) -> str:
        validate_identifier(owner, "owner")
        validate_identifier(repo, "repository")
        return self.source_dir_overrides.get(f"{owner}/{repo}", self.default_source_dir)

    def preview_credentials(self) -> tuple[str, str]:
        username = self.preview_username
        if self.preview_username_file is not None:
            username = self.preview_username_file.read_text(encoding="utf-8").strip()
        if self.preview_password_file is not None:
            password = self.preview_password_file.read_text(encoding="utf-8").strip()
        elif self.preview_password is not None:
            password = self.preview_password.get_secret_value()
        else:  # protected by model validation
            raise ValueError("preview password is unavailable")
        if not username or not password:
            raise ValueError("preview credential files must contain non-empty values")
        return username, password

from __future__ import annotations

from pathlib import Path

import pytest

from pr_html_preview.config import AppConfig


@pytest.fixture
def app_config(tmp_path: Path) -> AppConfig:
    return AppConfig(
        github_app_id=1,
        github_private_key="test-key",
        github_webhook_secret="webhook-secret",
        preview_base_url="https://preview.example.com",
        storage_root=tmp_path,
        preview_username="preview",
        preview_password="generated-test-password",
    )

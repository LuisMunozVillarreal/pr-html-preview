from __future__ import annotations

import base64
import hashlib
import hmac
import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from pr_html_preview.config import AppConfig
from pr_html_preview.main import (
    WebhookProcessor,
    _apply_live_snapshot,
    _durable_payload,
    _minimize_pull_request_payload,
    create_app,
)


class FakeProcessor:
    def __init__(self, error: Exception | None = None) -> None:
        self.calls: list[tuple[dict[str, Any], str]] = []
        self.error = error

    async def handle_webhook(self, payload: dict[str, Any], github_event: str) -> None:
        if self.error:
            raise self.error
        self.calls.append((payload, github_event))


def signature(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def webhook(client: TestClient, body: bytes, secret: str = "webhook-secret", **headers: str):
    default_headers = {
        "x-hub-signature-256": signature(secret, body),
        "x-github-event": "pull_request",
        "x-github-delivery": "delivery-1",
        "content-type": "application/json",
    }
    default_headers.update(headers)
    return client.post("/api/github/webhook", content=body, headers=default_headers)


def test_health_and_webhook_are_public_but_root_requires_auth(app_config: AppConfig) -> None:
    client = TestClient(create_app(app_config, FakeProcessor()))
    assert client.get("/healthz").status_code == 200
    unauthorized = client.get("/")
    assert unauthorized.status_code == 401
    assert unauthorized.headers["www-authenticate"].startswith("Basic")
    assert "No previews" not in unauthorized.text
    authorized = client.get("/", auth=("preview", "generated-test-password"))
    assert authorized.status_code == 200
    assert "PR HTML previews" in authorized.text
    assert authorized.headers["x-robots-tag"] == "noindex, nofollow"


def test_basic_auth_rejects_malformed_or_wrong_credentials(app_config: AppConfig) -> None:
    client = TestClient(create_app(app_config, FakeProcessor()))
    assert client.get("/", headers={"authorization": "Basic !!!"}).status_code == 401
    no_separator = base64.b64encode(b"preview-only").decode()
    assert client.get("/", headers={"authorization": f"Basic {no_separator}"}).status_code == 401
    assert client.get("/", auth=("preview", "wrong")).status_code == 401


def test_missing_mounted_credentials_fail_closed(app_config: AppConfig, tmp_path: Path) -> None:
    config = app_config.model_copy(update={"preview_password": None, "preview_password_file": tmp_path / "missing-password"})
    response = TestClient(create_app(config, FakeProcessor())).get("/")
    assert response.status_code == 503


def test_live_snapshot_rejects_unknown_pull_request_state() -> None:
    with pytest.raises(ValueError, match="state"):
        _apply_live_snapshot({"pull_request": {}}, {"state": "draft"})


def test_durable_payload_excludes_unneeded_user_content() -> None:
    payload = {
        "action": "opened",
        "number": 1,
        "installation": {"id": 7},
        "sender": {"login": "person"},
        "repository": {
            "full_name": "octo-org/example-repo",
            "name": "example-repo",
            "owner": {"login": "octo-org"},
        },
        "pull_request": {
            "number": 1,
            "title": "private title",
            "body": "private body",
            "head": {"ref": "main", "sha": "abcdef1234", "repo": {"full_name": "octo-org/example-repo"}},
            "base": {"repo": {"full_name": "octo-org/example-repo"}},
        },
    }
    minimized = _minimize_pull_request_payload(payload)
    serialized = json.dumps(minimized)
    assert "private title" not in serialized
    assert "private body" not in serialized
    assert "sender" not in minimized
    assert _durable_payload("ping", {"sender": {"login": "person"}}) == {}
    with pytest.raises(ValueError, match="pull_request"):
        _minimize_pull_request_payload({})


def test_default_processor_can_be_constructed_without_network_access(app_config: AppConfig) -> None:
    assert TestClient(create_app(app_config)).get("/healthz").status_code == 200


def test_production_dispatcher_queues_and_deduplicates_delivery(app_config: AppConfig) -> None:
    body = b"{}"
    with TestClient(create_app(app_config)) as client:
        first = webhook(client, body, **{"x-github-event": "push", "x-github-delivery": "stable-delivery"})
        second = webhook(client, body, **{"x-github-event": "push", "x-github-delivery": "stable-delivery"})
    assert first.status_code == 202
    assert first.json()["status"] == "queued"
    assert second.status_code == 202
    assert second.json()["status"] == "duplicate"


def test_valid_webhook_is_dispatched(app_config: AppConfig) -> None:
    processor = FakeProcessor()
    client = TestClient(create_app(app_config, processor))
    body = json.dumps({"action": "opened"}).encode()
    response = webhook(client, body)
    assert response.status_code == 200
    assert processor.calls == [({"action": "opened"}, "pull_request")]


def test_webhook_rejects_signature_json_size_and_processor_errors(app_config: AppConfig) -> None:
    client = TestClient(create_app(app_config, FakeProcessor()))
    body = b"{}"
    assert client.post("/api/github/webhook", content=body).status_code == 401
    assert webhook(client, b"not-json").status_code == 400
    assert webhook(client, b"[]").status_code == 400
    small = app_config.model_copy(update={"request_body_max_bytes": 2})
    assert webhook(TestClient(create_app(small, FakeProcessor())), b'{"x":1}').status_code == 413
    assert webhook(TestClient(create_app(app_config, FakeProcessor(ValueError("bad")))), body).status_code == 400
    assert webhook(TestClient(create_app(app_config, FakeProcessor(RuntimeError("bad")))), body).status_code == 500
    invalid_length = client.post(
        "/api/github/webhook",
        content=body,
        headers={"content-length": "invalid", "x-hub-signature-256": signature("webhook-secret", body)},
    )
    assert invalid_length.status_code == 400


def test_authenticated_static_serving_and_missing_asset_behavior(app_config: AppConfig) -> None:
    root = app_config.storage_root / "octo-org" / "example-repo" / "main"
    (root / "assets").mkdir(parents=True)
    (root / "index.html").write_text("<h1>preview</h1>")
    (root / "assets" / "site.css").write_text("body{}")
    client = TestClient(create_app(app_config, FakeProcessor()))
    auth = ("preview", "generated-test-password")
    assert client.get("/octo-org/example-repo/main/", auth=auth).text == "<h1>preview</h1>"
    assert client.get("/octo-org/example-repo/main/assets/site.css", auth=auth).status_code == 200
    assert client.get("/octo-org/example-repo/main/assets/", auth=auth).status_code == 404
    assert client.get("/octo-org/example-repo/main/missing.js", auth=auth).status_code == 404
    assert client.get("/octo-org/example-repo/main/%2e%2e/%2e%2e/secret", auth=auth).status_code == 404
    assert client.get("/bad%2fowner/example-repo/main/", auth=auth).status_code == 404
    listing = client.get("/", auth=auth)
    assert "octo-org/example-repo/main" in listing.text


def test_static_server_rejects_symlink_escape(app_config: AppConfig, tmp_path: Path) -> None:
    root = app_config.storage_root / "octo-org" / "example-repo" / "main"
    root.mkdir(parents=True)
    outside = tmp_path / "outside.txt"
    outside.write_text("secret")
    (root / "index.html").write_text("ok")
    (root / "link.txt").symlink_to(outside)
    client = TestClient(create_app(app_config, FakeProcessor()))
    response = client.get("/octo-org/example-repo/main/link.txt", auth=("preview", "generated-test-password"))
    assert response.status_code == 404
    assert "secret" not in response.text


@pytest.mark.asyncio
async def test_webhook_processor_ignores_other_events_and_rejects_missing_installation(app_config: AppConfig) -> None:
    processor = WebhookProcessor(app_config, object())  # type: ignore[arg-type]
    await processor.handle_webhook({}, "push")
    await processor.handle_webhook({"action": "labeled"}, "pull_request")
    with pytest.raises(ValueError, match="installation"):
        await processor.handle_webhook({"action": "opened"}, "pull_request")


@pytest.mark.asyncio
async def test_webhook_processor_closes_client_after_processing(app_config: AppConfig) -> None:
    class Preview:
        async def cleanup(self, owner: str, repo: str, branch: str) -> None:
            self.cleaned = (owner, repo, branch)

    class Client:
        def __init__(self) -> None:
            self.closed = False

        async def get_pull_request(self, owner: str, repo: str, number: int) -> dict[str, Any]:
            return event["pull_request"] | {"state": "closed"}

        async def upsert_check_run(self, **kwargs: Any) -> str:
            return "1"

        async def close(self) -> None:
            self.closed = True

    preview = Preview()
    client = Client()
    processor = WebhookProcessor(
        app_config,
        preview,  # type: ignore[arg-type]
        client_factory=lambda installation_id: client,  # type: ignore[return-value]
    )
    event = {
        "action": "closed",
        "number": 1,
        "installation": {"id": 7},
        "repository": {"full_name": "octo-org/example-repo", "name": "example-repo", "owner": {"login": "octo-org"}},
        "pull_request": {
            "number": 1,
            "head": {
                "ref": "main",
                "sha": "abcdef1234",
                "repo": {"full_name": "octo-org/example-repo"},
            },
            "base": {"repo": {"full_name": "octo-org/example-repo"}},
        },
    }
    await processor.handle_webhook(event, "pull_request")
    assert preview.cleaned == ("octo-org", "example-repo", "main")
    assert client.closed is True

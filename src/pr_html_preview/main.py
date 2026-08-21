from __future__ import annotations

import asyncio
import base64
import binascii
import hmac
import html
import json
import logging
import mimetypes
import os
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any, Protocol
from urllib.parse import quote

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

from .config import AppConfig, validate_identifier
from .github import GitHubApiClient
from .handlers import SUPPORTED_ACTIONS, process_pull_request_event
from .preview import PreviewService, deployment_lock
from .queue import EventStore, QueueWorker
from .webhook import validate_github_signature

LOGGER = logging.getLogger(__name__)
ClientFactory = Callable[[int], GitHubApiClient]


class EventProcessor(Protocol):
    async def handle_webhook(self, payload: dict[str, Any], github_event: str) -> None: ...


class WebhookProcessor:
    def __init__(
        self,
        config: AppConfig,
        preview_service: PreviewService,
        state_store: EventStore | None = None,
        client_factory: ClientFactory | None = None,
    ) -> None:
        self.config = config
        self.preview_service = preview_service
        self.state_store = state_store
        self.client_factory = client_factory or self._client

    def _client(self, installation_id: int) -> GitHubApiClient:
        return GitHubApiClient(
            self.config.github_app_id,
            self.config.github_private_key.get_secret_value(),
            installation_id,
        )

    async def handle_webhook(self, payload: dict[str, Any], github_event: str) -> None:
        if github_event != "pull_request":
            return
        if payload.get("action") not in SUPPORTED_ACTIONS:
            return
        try:
            installation_id = int(payload["installation"]["id"])
            owner = payload["repository"]["owner"]["login"]
            repo = payload["repository"]["name"]
            pr_number = int(payload.get("number", payload["pull_request"]["number"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("invalid GitHub App installation payload") from exc
        client = self.client_factory(installation_id)
        try:
            snapshot = await client.get_pull_request(owner, repo, pr_number)
            payload = _apply_live_snapshot(payload, snapshot)
            await process_pull_request_event(
                payload,
                self.preview_service,
                client,
                self.config,
                self.state_store,
            )
        finally:
            await client.close()


def _apply_live_snapshot(payload: dict[str, Any], snapshot: dict[str, Any]) -> dict[str, Any]:
    updated = dict(payload)
    pull_request = dict(payload["pull_request"])
    state = snapshot.get("state")
    if state not in {"open", "closed"}:
        raise ValueError("invalid live pull request state")
    updated["action"] = "closed" if state == "closed" else "synchronize"
    pull_request["head"] = snapshot["head"]
    pull_request["base"] = snapshot["base"]
    pull_request["number"] = snapshot["number"]
    updated["number"] = snapshot["number"]
    updated["pull_request"] = pull_request
    return updated


def _minimize_pull_request_payload(payload: dict[str, Any]) -> dict[str, Any]:
    try:
        pull_request = payload["pull_request"]
        repository = payload["repository"]
        return {
            "action": payload.get("action", "synchronize"),
            "number": int(payload.get("number", pull_request["number"])),
            "installation": {"id": int(payload["installation"]["id"])},
            "repository": {
                "full_name": repository["full_name"],
                "name": repository["name"],
                "owner": {"login": repository["owner"]["login"]},
            },
            "pull_request": {
                "number": int(pull_request["number"]),
                "head": {
                    "ref": pull_request["head"]["ref"],
                    "sha": pull_request["head"]["sha"],
                    "repo": {"full_name": pull_request["head"]["repo"]["full_name"]},
                },
                "base": {"repo": {"full_name": pull_request["base"]["repo"]["full_name"]}},
            },
        }
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("invalid pull_request webhook payload") from exc


def _durable_payload(github_event: str, payload: dict[str, Any]) -> dict[str, Any]:
    return _minimize_pull_request_payload(payload) if github_event == "pull_request" else {}


def _public_path(path: str) -> bool:
    return path in {"/healthz", "/api/github/webhook"}


def _basic_auth_ok(username: str, password: str, header: str | None) -> bool:
    if header is None or not header.startswith("Basic "):
        return False
    try:
        decoded = base64.b64decode(header[6:], validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return False
    supplied_username, separator, supplied_password = decoded.partition(":")
    if not separator:
        return False
    return hmac.compare_digest(supplied_username, username) & hmac.compare_digest(supplied_password, password)


async def _bounded_body(request: Request, limit: int) -> bytes:
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > limit:
                raise OverflowError
        except ValueError as exc:
            raise ValueError("invalid Content-Length") from exc
        except OverflowError as exc:
            raise OverflowError("request body too large") from exc
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > limit:
            raise OverflowError("request body too large")
    return bytes(body)


def _security_headers(response: Response) -> Response:
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault("X-Robots-Tag", "noindex, nofollow")
    response.headers.setdefault("Cache-Control", "private, no-store")
    return response


def create_app(config: AppConfig | None = None, processor: EventProcessor | None = None) -> FastAPI:
    cfg = config or AppConfig.load_from_env()
    storage = PreviewService(
        cfg.ensure_storage_root(),
        cfg.max_file_count,
        cfg.max_file_size_bytes,
        cfg.max_total_bytes,
        cfg.max_storage_bytes,
        cfg.max_preview_count,
        cfg.min_free_bytes,
    )
    store: EventStore | None = None
    worker: QueueWorker | None = None
    if processor is None:
        store = EventStore(cfg.storage_root / ".events.sqlite3")
        event_processor: EventProcessor = WebhookProcessor(cfg, storage, store)
        worker = QueueWorker(store, event_processor.handle_webhook)
    else:
        event_processor = processor

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        task: asyncio.Task[None] | None = None
        if worker is not None:
            task = asyncio.create_task(worker.run())
        try:
            yield
        finally:
            if worker is not None and task is not None:
                worker.stop()
                await task
            if store is not None:
                store.close()

    app = FastAPI(title="pr-html-preview", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)

    @app.middleware("http")
    async def security(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        if not _public_path(request.url.path):
            try:
                username, password = cfg.preview_credentials()
            except (OSError, ValueError):
                LOGGER.exception("preview credentials are unavailable")
                return _security_headers(Response("Service unavailable", status_code=503))
            if not _basic_auth_ok(username, password, request.headers.get("authorization")):
                return _security_headers(
                    Response("Unauthorized", status_code=401, headers={"WWW-Authenticate": 'Basic realm="PR HTML previews"'})
                )
        return _security_headers(await call_next(request))

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/api/github/webhook")
    async def github_webhook(request: Request) -> Response:
        try:
            body = await _bounded_body(request, cfg.request_body_max_bytes)
        except OverflowError:
            return Response("request too large", status_code=413)
        except ValueError:
            return Response("invalid request", status_code=400)
        if not validate_github_signature(cfg.github_webhook_secret.get_secret_value(), body, request.headers.get("x-hub-signature-256")):
            return Response("invalid signature", status_code=401)
        try:
            payload = json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return Response("invalid JSON", status_code=400)
        if not isinstance(payload, dict):
            return Response("invalid payload", status_code=400)
        github_event = request.headers.get("x-github-event", "")
        delivery_id = request.headers.get("x-github-delivery", "")
        if not delivery_id or len(delivery_id) > 128:
            return Response("invalid delivery identifier", status_code=400)
        if store is not None:
            try:
                queued_payload = _durable_payload(github_event, payload)
            except ValueError:
                return Response("invalid payload", status_code=400)
            inserted = store.enqueue(delivery_id, github_event, queued_payload)
            return JSONResponse({"status": "queued" if inserted else "duplicate"}, status_code=202)
        try:
            await event_processor.handle_webhook(payload, github_event)
        except ValueError:
            LOGGER.warning("rejected malformed webhook payload")
            return Response("invalid payload", status_code=400)
        except Exception:
            LOGGER.exception("webhook processing failed")
            return Response("processing failed", status_code=500)
        return JSONResponse({"status": "accepted"})

    @app.get("/")
    async def index() -> HTMLResponse:
        items = []
        for owner, repo, branch in storage.list_previews():
            url = f"/{quote(owner)}/{quote(repo)}/{quote(branch)}/"
            label = html.escape(f"{owner}/{repo}/{branch}")
            items.append(f'<li><a href="{url}">{label}</a></li>')
        listing = "".join(items) if items else "<li>No previews are currently published.</li>"
        page = (
            "<!doctype html><html><head><title>PR HTML previews</title></head>"
            f"<body><h1>PR HTML previews</h1><ul>{listing}</ul></body></html>"
        )
        return HTMLResponse(page)

    @app.get("/{owner}/{repo}/{branch_slug}/")
    @app.get("/{owner}/{repo}/{branch_slug}/{asset_path:path}")
    async def serve_preview(owner: str, repo: str, branch_slug: str, asset_path: str = "index.html") -> Response:
        try:
            validate_identifier(owner, "owner")
            validate_identifier(repo, "repository")
            validate_identifier(branch_slug, "branch slug")
        except ValueError:
            return Response("not found", status_code=404)
        root = (cfg.storage_root / owner / repo / branch_slug).resolve()
        requested = asset_path or "index.html"
        if "\\" in requested or requested.startswith("/"):
            return Response("not found", status_code=404)
        target = (root / requested).resolve()
        if root != target and root not in target.parents:
            return Response("not found", status_code=404)
        if target.is_dir():
            target = (target / "index.html").resolve()
        with deployment_lock(cfg.storage_root):
            if not target.is_file() or root not in target.parents:
                return Response("not found", status_code=404)
            content = target.read_bytes()
        media_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        return Response(content, media_type=media_type)

    return app


def main() -> None:  # pragma: no cover - exercised by the container entrypoint
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(levelname)s %(name)s %(message)s")
    import uvicorn

    uvicorn.run(
        create_app(),
        host=os.getenv("HOST", "0.0.0.0"),  # noqa: S104 - container listener must accept pod traffic
        port=int(os.getenv("PORT", "8080")),
    )


if __name__ == "__main__":  # pragma: no cover
    main()

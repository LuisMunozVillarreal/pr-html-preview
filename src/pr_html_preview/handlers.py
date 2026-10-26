from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Protocol

from .config import AppConfig, validate_identifier
from .preview import ContentTransportError, PathSafetyError
from .slug import slugify_ref

LOGGER = logging.getLogger(__name__)
SUPPORTED_ACTIONS = {"opened", "reopened", "synchronize", "closed"}


class PreviewLifecycle(Protocol):
    async def deploy(
        self,
        owner: str,
        repo: str,
        branch_ref: str,
        source_dir: str,
        head_sha: str,
        github: Any,
    ) -> str: ...

    async def cleanup(self, owner: str, repo: str, branch_ref: str) -> None: ...


class PreviewState(Protocol):
    def get_preview_state(self, owner: str, repo: str, pr_number: int) -> tuple[str, str] | None: ...

    def set_preview_state(self, owner: str, repo: str, pr_number: int, branch_ref: str, head_sha: str) -> None: ...

    def delete_preview_state(self, owner: str, repo: str, pr_number: int) -> None: ...

    def branch_in_use(self, owner: str, repo: str, branch_ref: str, excluding_pr: int) -> bool: ...


@dataclass(frozen=True)
class PullRequestEvent:
    action: str
    number: int
    owner: str
    repo: str
    branch_ref: str
    head_sha: str
    is_fork: bool


def parse_pull_request_event(payload: dict[str, Any]) -> PullRequestEvent | None:
    action = payload.get("action")
    if action not in SUPPORTED_ACTIONS:
        return None
    try:
        pr = payload["pull_request"]
        repository = payload["repository"]
        head = pr["head"]
        base = pr["base"]
        owner = validate_identifier(repository["owner"]["login"], "owner")
        repo = validate_identifier(repository["name"], "repository")
        full_name = repository["full_name"]
        number = int(payload.get("number", pr["number"]))
        branch_ref = head["ref"]
        head_sha = head["sha"]
        is_fork = head["repo"]["full_name"] != base["repo"]["full_name"]
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("invalid pull_request webhook payload") from exc
    if full_name != f"{owner}/{repo}" or not isinstance(branch_ref, str) or not isinstance(head_sha, str):
        raise ValueError("invalid pull_request webhook payload")
    if number <= 0 or len(head_sha) < 7:
        raise ValueError("invalid pull_request webhook payload")
    return PullRequestEvent(action, number, owner, repo, branch_ref, head_sha, is_fork)


async def _write_check(github: Any, event: PullRequestEvent, **values: Any) -> None:
    external_id = f"pr-{event.number}"
    finder = getattr(github, "find_check_run", None)
    check_run_id = await finder(event.owner, event.repo, event.head_sha, external_id) if finder else None
    await github.upsert_check_run(
        owner=event.owner,
        repo=event.repo,
        check_name="pr-html-preview",
        head_sha=event.head_sha,
        status="completed",
        external_id=external_id,
        check_run_id=check_run_id,
        **values,
    )


async def process_pull_request_event(
    payload: dict[str, Any],
    preview_service: PreviewLifecycle,
    github_client: Any,
    config: AppConfig,
    state_store: PreviewState | None = None,
) -> None:
    event = parse_pull_request_event(payload)
    if event is None:
        return
    previous = state_store.get_preview_state(event.owner, event.repo, event.number) if state_store else None

    if event.is_fork:
        await _write_check(
            github_client,
            event,
            conclusion="neutral",
            title="Preview skipped",
            summary="Fork previews are not supported by this service.",
            details_url=None,
        )
        return

    if event.action == "closed":
        branches = {event.branch_ref}
        if previous:
            branches.add(previous[0])
        for branch in branches:
            if state_store is None or not state_store.branch_in_use(event.owner, event.repo, branch, event.number):
                await preview_service.cleanup(event.owner, event.repo, branch)
        if state_store:
            state_store.delete_preview_state(event.owner, event.repo, event.number)
        await _write_check(
            github_client,
            event,
            conclusion="success",
            title="Preview removed",
            summary="The pull request preview was removed.",
            details_url=None,
        )
        return

    source_dir = config.source_dir_for(event.owner, event.repo)
    try:
        await preview_service.deploy(
            event.owner,
            event.repo,
            event.branch_ref,
            source_dir,
            event.head_sha,
            github_client,
        )
    except (ContentTransportError, PathSafetyError, ValueError):
        LOGGER.warning(
            "preview content was rejected",
            extra={"repository": f"{event.owner}/{event.repo}", "pr": event.number},
        )
        await _write_check(
            github_client,
            event,
            conclusion="failure",
            title="Preview failed",
            summary="The preview content was rejected. Check the service logs for the redacted error category.",
            details_url=None,
        )
        return
    except Exception:
        LOGGER.exception(
            "transient preview deployment failure",
            extra={"repository": f"{event.owner}/{event.repo}", "pr": event.number},
        )
        raise

    if (
        previous
        and previous[0] != event.branch_ref
        and (state_store is None or not state_store.branch_in_use(event.owner, event.repo, previous[0], event.number))
    ):
        await preview_service.cleanup(event.owner, event.repo, previous[0])
    if state_store:
        state_store.set_preview_state(event.owner, event.repo, event.number, event.branch_ref, event.head_sha)
    preview_url = f"{config.preview_base_url}/{event.owner}/{event.repo}/{slugify_ref(event.branch_ref)}/"
    await _write_check(
        github_client,
        event,
        conclusion="success",
        title="Preview ready",
        summary="The authenticated HTML preview is ready.",
        details_url=preview_url,
    )

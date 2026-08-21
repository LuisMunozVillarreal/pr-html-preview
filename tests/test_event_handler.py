from __future__ import annotations

from typing import Any

import pytest

from pr_html_preview.config import AppConfig
from pr_html_preview.handlers import parse_pull_request_event, process_pull_request_event
from pr_html_preview.preview import ContentTransportError


class FakePreview:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.deploy_calls: list[tuple[Any, ...]] = []
        self.cleanup_calls: list[tuple[Any, ...]] = []

    async def deploy(self, *args: Any) -> str:
        if self.fail:
            raise ContentTransportError("sensitive internal detail")
        self.deploy_calls.append(args[:-1])
        return "feature-x"

    async def cleanup(self, *args: Any) -> None:
        self.cleanup_calls.append(args)


class FakeGitHub:
    def __init__(self) -> None:
        self.checks: list[dict[str, Any]] = []

    async def upsert_check_run(self, **kwargs: Any) -> str:
        self.checks.append(kwargs)
        return "1"


class FakeState:
    def __init__(self, previous: tuple[str, str] | None = None, shared: set[str] | None = None) -> None:
        self.previous = previous
        self.shared = shared or set()
        self.saved: tuple[str, str, int, str, str] | None = None
        self.deleted = False

    def get_preview_state(self, owner: str, repo: str, pr_number: int):
        return self.previous

    def set_preview_state(self, owner: str, repo: str, pr_number: int, branch_ref: str, head_sha: str) -> None:
        self.saved = (owner, repo, pr_number, branch_ref, head_sha)

    def delete_preview_state(self, owner: str, repo: str, pr_number: int) -> None:
        self.deleted = True

    def branch_in_use(self, owner: str, repo: str, branch_ref: str, excluding_pr: int) -> bool:
        return branch_ref in self.shared


def payload(action: str = "opened", *, fork: bool = False) -> dict[str, Any]:
    head_full_name = "contributor/fork" if fork else "octo-org/example-repo"
    return {
        "action": action,
        "number": 4,
        "installation": {"id": 7},
        "repository": {"full_name": "octo-org/example-repo", "owner": {"login": "octo-org"}, "name": "example-repo"},
        "pull_request": {
            "number": 4,
            "head": {
                "ref": "feature/x",
                "sha": "abcdef123456",
                "repo": {"full_name": head_full_name, "owner": {"login": "octo-org"}, "name": "example-repo"},
            },
            "base": {"repo": {"full_name": "octo-org/example-repo", "owner": {"login": "octo-org"}, "name": "example-repo"}},
        },
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["opened", "reopened", "synchronize"])
async def test_deploy_actions_publish_successful_check(action: str, app_config: AppConfig) -> None:
    preview = FakePreview()
    github = FakeGitHub()
    await process_pull_request_event(payload(action), preview, github, app_config)
    assert preview.deploy_calls == [("octo-org", "example-repo", "feature/x", "docs/html", "abcdef123456")]
    assert github.checks[0]["conclusion"] == "success"
    assert github.checks[0]["details_url"].startswith("https://preview.example.com/octo-org/example-repo/feature-x-")


@pytest.mark.asyncio
async def test_closed_action_cleans_preview(app_config: AppConfig) -> None:
    preview = FakePreview()
    github = FakeGitHub()
    state = FakeState(("feature/old", "oldsha12"))
    await process_pull_request_event(payload("closed"), preview, github, app_config, state)
    assert set(preview.cleanup_calls) == {
        ("octo-org", "example-repo", "feature/old"),
        ("octo-org", "example-repo", "feature/x"),
    }
    assert state.deleted is True
    assert github.checks[0]["title"] == "Preview removed"


@pytest.mark.asyncio
async def test_fork_is_skipped_by_default(app_config: AppConfig) -> None:
    preview = FakePreview()
    github = FakeGitHub()
    await process_pull_request_event(payload(fork=True), preview, github, app_config)
    assert preview.deploy_calls == []
    assert github.checks[0]["conclusion"] == "neutral"


@pytest.mark.asyncio
async def test_failure_check_does_not_expose_exception_text(app_config: AppConfig) -> None:
    github = FakeGitHub()
    await process_pull_request_event(payload(), FakePreview(fail=True), github, app_config)
    assert github.checks[0]["conclusion"] == "failure"
    assert "sensitive" not in github.checks[0]["summary"]


@pytest.mark.asyncio
async def test_transient_deployment_failure_is_retried_by_queue(app_config: AppConfig) -> None:
    class TransientPreview(FakePreview):
        async def deploy(self, *args: Any) -> str:
            raise RuntimeError("temporary network failure")

    github = FakeGitHub()
    with pytest.raises(RuntimeError, match="temporary"):
        await process_pull_request_event(payload(), TransientPreview(), github, app_config)
    assert github.checks == []


@pytest.mark.asyncio
async def test_unsupported_action_does_nothing(app_config: AppConfig) -> None:
    preview = FakePreview()
    github = FakeGitHub()
    await process_pull_request_event(payload("edited"), preview, github, app_config)
    assert preview.deploy_calls == []
    assert github.checks == []


def test_payload_parser_rejects_malformed_repository() -> None:
    malformed = payload()
    malformed["repository"]["full_name"] = "different/repo"
    with pytest.raises(ValueError, match="invalid pull_request"):
        parse_pull_request_event(malformed)


@pytest.mark.asyncio
async def test_branch_rename_removes_previous_preview_and_updates_pr_state(app_config: AppConfig) -> None:
    preview = FakePreview()
    github = FakeGitHub()
    state = FakeState(("feature/old", "oldsha12"))
    await process_pull_request_event(payload(), preview, github, app_config, state)
    assert preview.cleanup_calls == [("octo-org", "example-repo", "feature/old")]
    assert state.saved == ("octo-org", "example-repo", 4, "feature/x", "abcdef123456")


@pytest.mark.asyncio
async def test_closing_pr_does_not_remove_branch_shared_by_another_pr(app_config: AppConfig) -> None:
    preview = FakePreview()
    state = FakeState(("feature/x", "abcdef123456"), shared={"feature/x"})
    await process_pull_request_event(payload("closed"), preview, FakeGitHub(), app_config, state)
    assert preview.cleanup_calls == []
    assert state.deleted is True

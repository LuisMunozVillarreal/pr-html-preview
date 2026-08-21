from __future__ import annotations

import os
from pathlib import Path

import pytest

import pr_html_preview.preview as preview_module
from pr_html_preview.preview import (
    ContentTransportError,
    PathSafetyError,
    PreviewService,
    TreeResponse,
    _normalize_blob_payload,
)
from pr_html_preview.slug import slugify_ref


class FakeGitHub:
    def __init__(self, entries: list[dict[str, object]], blobs: dict[str, bytes], truncated: bool = False) -> None:
        self.response = TreeResponse(entries, truncated)
        self.blobs = blobs

    async def get_tree(self, owner: str, repo: str, ref: str) -> TreeResponse:
        return self.response

    async def get_blob(self, owner: str, repo: str, blob_sha: str) -> bytes:
        return self.blobs[blob_sha]


def entry(path: str, sha: str = "1", *, mode: str = "100644", kind: str = "blob", size: int = 0) -> dict[str, object]:
    return {"path": path, "sha": sha, "mode": mode, "type": kind, "size": size}


@pytest.mark.asyncio
async def test_deploy_and_replace_preview_atomically(tmp_path: Path) -> None:
    github = FakeGitHub(
        [entry("docs/html/index.html", size=2), entry("docs/html/assets/site.css", "2", size=3)],
        {"1": b"v1", "2": b"css"},
    )
    service = PreviewService(tmp_path)
    main_slug = slugify_ref("main")
    assert await service.deploy("octo-org", "example-repo", "main", "docs/html", "abc1234", github) == main_slug
    target = tmp_path / "octo-org" / "example-repo" / main_slug
    assert (target / "index.html").read_bytes() == b"v1"
    github.blobs["1"] = b"v2"
    await service.deploy("octo-org", "example-repo", "main", "docs/html", "def5678", github)
    assert (target / "index.html").read_bytes() == b"v2"
    assert service.list_previews() == [("octo-org", "example-repo", main_slug)]


@pytest.mark.asyncio
async def test_failed_atomic_swap_restores_existing_preview(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    github = FakeGitHub([entry("docs/html/index.html", size=3)], {"1": b"old"})
    service = PreviewService(tmp_path)
    await service.deploy("octo-org", "example-repo", "main", "docs/html", "abc1234", github)
    github.blobs["1"] = b"new"
    real_replace = os.replace
    calls = 0

    def fail_new_publish(source: str | Path, destination: str | Path) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("synthetic swap failure")
        real_replace(source, destination)

    monkeypatch.setattr(preview_module.os, "replace", fail_new_publish)
    with pytest.raises(OSError, match="synthetic"):
        await service.deploy("octo-org", "example-repo", "main", "docs/html", "def5678", github)
    assert (tmp_path / "octo-org" / "example-repo" / slugify_ref("main") / "index.html").read_bytes() == b"old"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad_entry",
    [
        entry("docs/html/link", mode="120000"),
        entry("docs/html/module", kind="commit", mode="160000"),
        entry("docs/html/../secret"),
        entry("docs/html/file", mode="100600"),
    ],
)
async def test_deploy_rejects_unsafe_entries(tmp_path: Path, bad_entry: dict[str, object]) -> None:
    service = PreviewService(tmp_path)
    with pytest.raises(PathSafetyError):
        await service.deploy("octo-org", "example-repo", "main", "docs/html", "abc1234", FakeGitHub([bad_entry], {"1": b"x"}))


@pytest.mark.asyncio
async def test_submodule_outside_source_directory_is_ignored(tmp_path: Path) -> None:
    github = FakeGitHub([entry("vendor/module", kind="commit", mode="160000"), entry("docs/html/index.html", size=2)], {"1": b"ok"})
    await PreviewService(tmp_path).deploy("octo-org", "example-repo", "main", "docs/html", "abc1234", github)


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["count", "file size", "total", "truncated"])
async def test_deploy_enforces_limits(tmp_path: Path, case: str) -> None:
    if case == "count":
        service = PreviewService(tmp_path, max_file_count=1)
        github = FakeGitHub([entry("docs/html/index.html"), entry("docs/html/a")], {"1": b"x"})
    elif case == "file size":
        service = PreviewService(tmp_path, max_file_size_bytes=1)
        github = FakeGitHub([entry("docs/html/index.html", size=2)], {"1": b"xx"})
    elif case == "total":
        service = PreviewService(tmp_path, max_total_bytes=1)
        github = FakeGitHub([entry("docs/html/index.html", size=2)], {"1": b"xx"})
    else:
        service = PreviewService(tmp_path)
        github = FakeGitHub([], {}, truncated=True)
    with pytest.raises(ContentTransportError, match=case):
        await service.deploy("octo-org", "example-repo", "main", "docs/html", "abc1234", github)


@pytest.mark.asyncio
async def test_deploy_requires_index_and_matching_download_size(tmp_path: Path) -> None:
    service = PreviewService(tmp_path)
    with pytest.raises(ContentTransportError, match="index"):
        await service.deploy("octo-org", "example-repo", "main", "docs/html", "abc1234", FakeGitHub([entry("docs/html/a")], {"1": b"x"}))
    with pytest.raises(ContentTransportError, match="declared"):
        await service.deploy(
            "octo-org", "example-repo", "main", "docs/html", "abc1234", FakeGitHub([entry("docs/html/index.html", size=2)], {"1": b"x"})
        )


@pytest.mark.asyncio
async def test_cleanup_is_idempotent_and_prunes_empty_directories(tmp_path: Path) -> None:
    service = PreviewService(tmp_path)
    github = FakeGitHub([entry("docs/html/index.html", size=1)], {"1": b"x"})
    await service.deploy("octo-org", "example-repo", "feature/x", "docs/html", "abc1234", github)
    await service.cleanup("octo-org", "example-repo", "feature/x")
    await service.cleanup("octo-org", "example-repo", "feature/x")
    assert service.list_previews() == []


@pytest.mark.asyncio
async def test_deploy_rejects_unsafe_owner_or_source_directory(tmp_path: Path) -> None:
    service = PreviewService(tmp_path)
    github = FakeGitHub([], {})
    with pytest.raises(ValueError):
        await service.deploy("../owner", "repo", "main", "docs/html", "abc1234", github)
    with pytest.raises(ValueError):
        await service.deploy("owner", "repo", "main", "../docs", "abc1234", github)


@pytest.mark.asyncio
async def test_deploy_rejects_malformed_tree_response_and_entries(tmp_path: Path) -> None:
    class BadResponseGitHub(FakeGitHub):
        async def get_tree(self, owner: str, repo: str, ref: str):
            return object()

    with pytest.raises(ContentTransportError, match="unsupported"):
        await PreviewService(tmp_path).deploy("owner", "repo", "main", "docs/html", "abc1234", BadResponseGitHub([], {}))
    malformed = FakeGitHub([{"path": "docs/html/index.html"}], {})
    with pytest.raises(ContentTransportError, match="required field"):
        await PreviewService(tmp_path).deploy("owner", "repo", "main", "docs/html", "abc1234", malformed)


def test_blob_payload_helper_is_strict() -> None:
    assert _normalize_blob_payload({"encoding": "base64", "content": "eA=="}) == b"x"
    with pytest.raises(ContentTransportError, match="base64"):
        _normalize_blob_payload({"encoding": "base64", "content": "!!!"})
    with pytest.raises(ContentTransportError, match="unsupported"):
        _normalize_blob_payload("plain text")


@pytest.mark.asyncio
async def test_deploy_enforces_aggregate_count_and_free_space(tmp_path: Path) -> None:
    github = FakeGitHub([entry("docs/html/index.html", size=1)], {"1": b"x"})
    count_limited = PreviewService(tmp_path / "count", max_preview_count=1)
    await count_limited.deploy("owner", "repo", "main", "docs/html", "abc1234", github)
    with pytest.raises(ContentTransportError, match="preview count"):
        await count_limited.deploy("owner", "repo", "other", "docs/html", "def5678", github)

    storage_limited = PreviewService(tmp_path / "storage", max_storage_bytes=0)
    with pytest.raises(ContentTransportError, match="aggregate"):
        await storage_limited.deploy("owner", "repo", "main", "docs/html", "abc1234", github)

    free_limited = PreviewService(tmp_path / "free", min_free_bytes=10**30)
    with pytest.raises(ContentTransportError, match="free storage"):
        await free_limited.deploy("owner", "repo", "main", "docs/html", "abc1234", github)


def test_startup_recovers_backup_and_removes_staging(tmp_path: Path) -> None:
    parent = tmp_path / "owner" / "repo"
    backup = parent / ".main.backup-test"
    staging = parent / ".main.tmp-test"
    backup.mkdir(parents=True)
    staging.mkdir()
    (backup / "index.html").write_text("old")
    PreviewService(tmp_path)
    assert (parent / "main" / "index.html").read_text() == "old"
    assert not staging.exists()

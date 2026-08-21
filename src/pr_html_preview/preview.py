from __future__ import annotations

import base64
import binascii
import fcntl
import os
import shutil
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Protocol, cast

from .config import normalize_source_dir, validate_identifier
from .slug import slugify_ref

ALLOWED_BLOB_MODES = {"100644", "100755"}


class PathSafetyError(ValueError):
    """Repository content attempted to escape the configured preview root."""


class ContentTransportError(ValueError):
    """Repository content could not be safely published."""


@dataclass(frozen=True)
class TreeResponse:
    entries: list[dict[str, object]]
    truncated: bool = False


@dataclass(frozen=True)
class BlobEntry:
    path: str
    mode: str
    type: str
    size: int
    sha: str


class GitHubTransport(Protocol):
    async def get_tree(self, owner: str, repo: str, ref: str) -> TreeResponse: ...

    async def get_blob(self, owner: str, repo: str, blob_sha: str) -> bytes: ...


class PreviewService:
    def __init__(
        self,
        storage_root: str | Path,
        max_file_count: int = 2000,
        max_file_size_bytes: int = 2 * 1024 * 1024,
        max_total_bytes: int = 150 * 1024 * 1024,
        max_storage_bytes: int = 750 * 1024 * 1024,
        max_preview_count: int = 100,
        min_free_bytes: int = 100 * 1024 * 1024,
    ) -> None:
        self.storage_root = Path(storage_root).resolve()
        self.max_file_count = max_file_count
        self.max_file_size_bytes = max_file_size_bytes
        self.max_total_bytes = max_total_bytes
        self.max_storage_bytes = max_storage_bytes
        self.max_preview_count = max_preview_count
        self.min_free_bytes = min_free_bytes
        self.storage_root.mkdir(parents=True, exist_ok=True)
        self._recover_orphans()

    async def deploy(
        self,
        owner: str,
        repo: str,
        branch_ref: str,
        source_dir: str,
        head_sha: str,
        github: GitHubTransport,
    ) -> str:
        owner = validate_identifier(owner, "owner")
        repo = validate_identifier(repo, "repository")
        source_prefix = normalize_source_dir(source_dir)
        branch_slug = slugify_ref(branch_ref)
        response = await github.get_tree(owner, repo, head_sha)
        if isinstance(response, list):  # compatibility for simple third-party transports
            response = TreeResponse(entries=response)
        if not isinstance(response, TreeResponse):
            entries = getattr(response, "entries", None)
            truncated = getattr(response, "truncated", None)
            if not isinstance(entries, list) or not isinstance(truncated, bool):
                raise ContentTransportError("unsupported tree response")
            response = TreeResponse(entries=entries, truncated=truncated)
        if response.truncated:
            raise ContentTransportError("GitHub tree response was truncated")

        selected: list[tuple[BlobEntry, PurePosixPath]] = []
        prefix = f"{source_prefix}/"
        for node in response.entries:
            entry = _to_entry(node)
            if not entry.path.startswith(prefix):
                continue
            relative_text = entry.path[len(prefix) :]
            relative = _safe_relative_path(relative_text)
            if entry.type == "commit":
                raise PathSafetyError("submodules are not allowed in the preview directory")
            if entry.type != "blob":
                continue
            if entry.mode == "120000":
                raise PathSafetyError("symbolic links are not allowed in the preview directory")
            if entry.mode not in ALLOWED_BLOB_MODES:
                raise PathSafetyError("unsupported file mode in preview directory")
            if entry.size > self.max_file_size_bytes:
                raise ContentTransportError("file size limit exceeded")
            selected.append((entry, relative))

        if not selected:
            raise ContentTransportError("configured preview directory contains no files")
        if len(selected) > self.max_file_count:
            raise ContentTransportError("file count limit exceeded")
        declared_total = sum(entry.size for entry, _ in selected)
        if declared_total > self.max_total_bytes:
            raise ContentTransportError("total preview size limit exceeded")
        if shutil.disk_usage(self.storage_root).free < declared_total + self.min_free_bytes:
            raise ContentTransportError("insufficient free storage for preview staging")

        target = self.preview_path(owner, repo, branch_ref)
        target.parent.mkdir(parents=True, exist_ok=True)
        staging = target.parent / f".{target.name}.tmp-{uuid.uuid4().hex}"
        staging.mkdir(mode=0o750)
        total = 0
        try:
            for entry, relative in selected:
                data = await github.get_blob(owner, repo, entry.sha)
                if not isinstance(data, bytes):
                    data = _normalize_blob_payload(data)
                if len(data) > self.max_file_size_bytes or (entry.size != 0 and len(data) != entry.size):
                    raise ContentTransportError("downloaded file size did not match the declared safe size")
                total += len(data)
                if total > self.max_total_bytes:
                    raise ContentTransportError("total preview size limit exceeded")
                destination = staging.joinpath(*relative.parts)
                destination.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
                destination.write_bytes(data)
                destination.chmod(0o640)
            if not (staging / "index.html").is_file():
                raise ContentTransportError("preview directory must contain index.html")
            with deployment_lock(self.storage_root):
                preview_count = len(self.list_previews())
                if not target.exists() and preview_count >= self.max_preview_count:
                    raise ContentTransportError("preview count limit exceeded")
                current_total = _directory_size(self.storage_root)
                old_size = _directory_size(target) if target.exists() else 0
                if current_total - old_size > self.max_storage_bytes:
                    raise ContentTransportError("aggregate preview storage limit exceeded")
            self._swap(staging, target)
        finally:
            shutil.rmtree(staging, ignore_errors=True)
        return branch_slug

    def _swap(self, staging: Path, target: Path) -> None:
        with deployment_lock(self.storage_root):
            backup = target.parent / f".{target.name}.backup-{uuid.uuid4().hex}"
            had_target = target.exists()
            if had_target:
                os.replace(target, backup)
            try:
                os.replace(staging, target)
            except Exception:
                if had_target and backup.exists():
                    os.replace(backup, target)
                raise
            finally:
                shutil.rmtree(backup, ignore_errors=True)

    async def cleanup(self, owner: str, repo: str, branch_ref: str) -> None:
        target = self.preview_path(owner, repo, branch_ref)
        with deployment_lock(self.storage_root):
            shutil.rmtree(target, ignore_errors=True)
            _remove_empty_parents(target.parent, self.storage_root)

    def preview_path(self, owner: str, repo: str, branch_ref: str) -> Path:
        owner = validate_identifier(owner, "owner")
        repo = validate_identifier(repo, "repository")
        return self.storage_root / owner / repo / slugify_ref(branch_ref)

    def list_previews(self) -> list[tuple[str, str, str]]:
        previews: list[tuple[str, str, str]] = []
        for index in self.storage_root.glob("*/*/*/index.html"):
            try:
                relative = index.relative_to(self.storage_root)
            except ValueError:
                continue
            if len(relative.parts) == 4 and not relative.parts[2].startswith("."):
                previews.append((relative.parts[0], relative.parts[1], relative.parts[2]))
        return sorted(previews)

    def _recover_orphans(self) -> None:
        with deployment_lock(self.storage_root):
            for directory in self.storage_root.glob("*/*/.*.tmp-*"):
                if directory.is_dir():
                    shutil.rmtree(directory, ignore_errors=True)
            for backup in self.storage_root.glob("*/*/.*.backup-*"):
                if not backup.is_dir():
                    continue
                slug = backup.name[1:].split(".backup-", 1)[0]
                target = backup.parent / slug
                if target.exists():
                    shutil.rmtree(backup, ignore_errors=True)
                else:
                    os.replace(backup, target)


def _remove_empty_parents(start: Path, stop: Path) -> None:
    current = start
    while current != stop:
        try:
            current.rmdir()
        except OSError:
            break
        current = current.parent


def _directory_size(path: Path) -> int:
    if not path.exists():
        return 0
    total = 0
    for item in path.rglob("*"):
        if item.is_file() and not item.is_symlink():
            total += item.stat().st_size
    return total


def _safe_relative_path(value: str) -> PurePosixPath:
    if not value or "\\" in value or value.startswith("/") or any(ord(char) < 32 for char in value):
        raise PathSafetyError("unsafe repository path")
    path = PurePosixPath(value)
    if any(part in {"", ".", ".."} for part in path.parts):
        raise PathSafetyError("unsafe repository path")
    return path


def _to_entry(node: dict[str, object]) -> BlobEntry:
    try:
        path = node["path"]
        mode = node["mode"]
        entry_type = node["type"]
        sha = node["sha"]
        size = node.get("size", 0)
    except KeyError as exc:
        raise ContentTransportError("tree entry omitted a required field") from exc
    if not all(isinstance(value, str) for value in (path, mode, entry_type, sha)):
        raise ContentTransportError("tree entry fields had invalid types")
    if not isinstance(size, int) or size < 0:
        raise ContentTransportError("tree entry size was invalid")
    return BlobEntry(
        path=cast(str, path),
        mode=cast(str, mode),
        type=cast(str, entry_type),
        size=size,
        sha=cast(str, sha),
    )


def _normalize_blob_payload(payload: Any) -> bytes:
    if isinstance(payload, bytes):
        return payload
    if not isinstance(payload, dict) or payload.get("encoding") != "base64" or not isinstance(payload.get("content"), str):
        raise ContentTransportError("unsupported blob payload")
    try:
        return base64.b64decode(payload["content"], validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ContentTransportError("invalid base64 blob payload") from exc


@contextmanager
def deployment_lock(storage_root: Path) -> Iterator[None]:
    lock_file = storage_root / ".deploy.lock"
    with lock_file.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)

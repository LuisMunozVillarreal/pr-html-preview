from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from pr_html_preview.queue import EventStore, QueueWorker


def test_event_store_deduplicates_and_tracks_preview_state(tmp_path: Path) -> None:
    store = EventStore(tmp_path / "queue.sqlite3")
    assert store.enqueue("delivery-1", "pull_request", {"action": "opened"}) is True
    assert store.enqueue("delivery-1", "pull_request", {"action": "opened"}) is False
    delivery = store.claim()
    assert delivery is not None
    assert delivery.delivery_id == "delivery-1"
    assert delivery.payload == {"action": "opened"}
    store.complete(delivery.row_id)
    assert store.claim() is None

    assert store.get_preview_state("octo-org", "example-repo", 1) is None
    store.set_preview_state("octo-org", "example-repo", 1, "feature/x", "abc1234")
    assert store.get_preview_state("octo-org", "example-repo", 1) == ("feature/x", "abc1234")
    store.set_preview_state("octo-org", "example-repo", 1, "feature/y", "def5678")
    assert store.get_preview_state("octo-org", "example-repo", 1) == ("feature/y", "def5678")
    store.set_preview_state("octo-org", "example-repo", 2, "feature/y", "other123")
    assert store.branch_in_use("octo-org", "example-repo", "feature/y", excluding_pr=1) is True
    store.delete_preview_state("octo-org", "example-repo", 1)
    assert store.get_preview_state("octo-org", "example-repo", 1) is None
    store.close()


def test_event_store_schedules_retry_and_discards_malformed_payload(tmp_path: Path) -> None:
    store = EventStore(tmp_path / "queue.sqlite3")
    store.enqueue("retry-delivery", "pull_request", {"number": 1})
    delivery = store.claim()
    assert delivery is not None
    store.retry(delivery, max_attempts=5)
    assert store.claim() is None

    store._connection.execute(  # noqa: SLF001 - corruption-path contract test
        "INSERT INTO deliveries(delivery_id, github_event, payload, created_at) VALUES (?, ?, ?, ?)",
        ("bad-delivery", "pull_request", "[]", 0),
    )
    assert store.claim() is None
    store.close()


def test_completed_delivery_retention_prunes_without_restart(tmp_path: Path) -> None:
    store = EventStore(tmp_path / "queue.sqlite3")
    store.enqueue("old-delivery", "push", {})
    delivery = store.claim()
    assert delivery is not None
    store.complete(delivery.row_id)
    store._connection.execute(  # noqa: SLF001 - retention contract test
        "UPDATE deliveries SET created_at=0 WHERE delivery_id='old-delivery'"
    )
    assert store.enqueue("new-delivery", "push", {}) is True
    assert store.enqueue("old-delivery", "push", {}) is True
    store.close()


@pytest.mark.asyncio
async def test_queue_worker_completes_delivery(tmp_path: Path) -> None:
    store = EventStore(tmp_path / "queue.sqlite3")
    handled: list[tuple[dict[str, object], str]] = []

    async def handler(payload: dict[str, object], event: str) -> None:
        handled.append((payload, event))

    store.enqueue("delivery-1", "pull_request", {"number": 1})
    worker = QueueWorker(store, handler, idle_seconds=0.01)
    task = asyncio.create_task(worker.run())
    for _ in range(50):
        if handled:
            break
        await asyncio.sleep(0.01)
    worker.stop()
    await task
    assert handled == [({"number": 1}, "pull_request")]
    assert store.claim() is None
    store.close()


@pytest.mark.asyncio
async def test_queue_worker_retains_delivery_at_maximum_backoff(tmp_path: Path) -> None:
    store = EventStore(tmp_path / "queue.sqlite3")

    calls = 0

    async def failing_handler(payload: dict[str, object], event: str) -> None:
        nonlocal calls
        calls += 1
        raise RuntimeError("synthetic failure")

    store.enqueue("delivery-1", "pull_request", {"number": 1})
    worker = QueueWorker(store, failing_handler, max_attempts=1, idle_seconds=0.01)
    task = asyncio.create_task(worker.run())
    for _ in range(50):
        if calls:
            break
        await asyncio.sleep(0.01)
    worker.stop()
    await task
    assert calls == 1
    assert store.claim() is None
    status = store._connection.execute(  # noqa: SLF001 - durability contract test
        "SELECT status FROM deliveries WHERE delivery_id='delivery-1'"
    ).fetchone()[0]
    assert status == "pending"
    store.close()

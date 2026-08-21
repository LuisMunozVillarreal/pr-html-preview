from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class QueuedDelivery:
    row_id: int
    delivery_id: str
    github_event: str
    payload: dict[str, Any]
    attempts: int


class EventStore:
    """Durable single-process queue and PR preview state stored on the shared PVC."""

    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path
        database_path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(database_path, check_same_thread=False, isolation_level=None)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA synchronous=FULL")
        self._connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS deliveries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                delivery_id TEXT NOT NULL UNIQUE,
                github_event TEXT NOT NULL,
                payload TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0,
                available_at REAL NOT NULL DEFAULT 0,
                created_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS deliveries_pending
                ON deliveries(status, available_at, id);
            CREATE TABLE IF NOT EXISTS preview_state (
                owner TEXT NOT NULL,
                repo TEXT NOT NULL,
                pr_number INTEGER NOT NULL,
                branch_ref TEXT NOT NULL,
                head_sha TEXT NOT NULL,
                PRIMARY KEY(owner, repo, pr_number)
            );
            """
        )
        self._connection.execute("UPDATE deliveries SET status='pending' WHERE status='processing'")
        self._prune_completed()

    def close(self) -> None:
        self._connection.close()

    def enqueue(self, delivery_id: str, github_event: str, payload: dict[str, Any]) -> bool:
        self._prune_completed()
        serialized = json.dumps(payload, separators=(",", ":"), sort_keys=True)
        try:
            self._connection.execute(
                "INSERT INTO deliveries(delivery_id, github_event, payload, created_at) VALUES (?, ?, ?, ?)",
                (delivery_id, github_event, serialized, time.time()),
            )
        except sqlite3.IntegrityError:
            return False
        return True

    def _prune_completed(self) -> None:
        self._connection.execute(
            "DELETE FROM deliveries WHERE status='done' AND created_at<?",
            (time.time() - 7 * 24 * 60 * 60,),
        )

    def claim(self) -> QueuedDelivery | None:
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            row = self._connection.execute(
                "SELECT * FROM deliveries WHERE status='pending' AND available_at<=? ORDER BY id LIMIT 1",
                (time.time(),),
            ).fetchone()
            if row is None:
                self._connection.execute("COMMIT")
                return None
            self._connection.execute(
                "UPDATE deliveries SET status='processing', attempts=attempts+1 WHERE id=?",
                (row["id"],),
            )
            self._connection.execute("COMMIT")
        except Exception:
            self._connection.execute("ROLLBACK")
            raise
        payload = json.loads(row["payload"])
        if not isinstance(payload, dict):
            self.complete(row["id"])
            return None
        return QueuedDelivery(row["id"], row["delivery_id"], row["github_event"], payload, row["attempts"] + 1)

    def complete(self, row_id: int) -> None:
        self._connection.execute("UPDATE deliveries SET status='done', payload='{}' WHERE id=?", (row_id,))

    def retry(self, delivery: QueuedDelivery, max_attempts: int) -> None:
        if delivery.attempts >= max_attempts:
            LOGGER.error(
                "webhook delivery remains pending at maximum backoff",
                extra={"delivery": delivery.delivery_id},
            )
            delay = 300
        else:
            delay = min(300, 2 ** min(delivery.attempts, 8))
        self._connection.execute(
            "UPDATE deliveries SET status='pending', available_at=? WHERE id=?",
            (time.time() + delay, delivery.row_id),
        )

    def get_preview_state(self, owner: str, repo: str, pr_number: int) -> tuple[str, str] | None:
        row = self._connection.execute(
            "SELECT branch_ref, head_sha FROM preview_state WHERE owner=? AND repo=? AND pr_number=?",
            (owner, repo, pr_number),
        ).fetchone()
        return None if row is None else (row["branch_ref"], row["head_sha"])

    def set_preview_state(self, owner: str, repo: str, pr_number: int, branch_ref: str, head_sha: str) -> None:
        self._connection.execute(
            """
            INSERT INTO preview_state(owner, repo, pr_number, branch_ref, head_sha) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(owner, repo, pr_number) DO UPDATE SET branch_ref=excluded.branch_ref, head_sha=excluded.head_sha
            """,
            (owner, repo, pr_number, branch_ref, head_sha),
        )

    def delete_preview_state(self, owner: str, repo: str, pr_number: int) -> None:
        self._connection.execute(
            "DELETE FROM preview_state WHERE owner=? AND repo=? AND pr_number=?",
            (owner, repo, pr_number),
        )

    def branch_in_use(self, owner: str, repo: str, branch_ref: str, excluding_pr: int) -> bool:
        row = self._connection.execute(
            "SELECT 1 FROM preview_state WHERE owner=? AND repo=? AND branch_ref=? AND pr_number<>? LIMIT 1",
            (owner, repo, branch_ref, excluding_pr),
        ).fetchone()
        return row is not None


class QueueWorker:
    def __init__(
        self,
        store: EventStore,
        handler: Callable[[dict[str, Any], str], Awaitable[None]],
        *,
        max_attempts: int = 5,
        idle_seconds: float = 0.25,
    ) -> None:
        self.store = store
        self.handler = handler
        self.max_attempts = max_attempts
        self.idle_seconds = idle_seconds
        self._stopping = asyncio.Event()

    def stop(self) -> None:
        self._stopping.set()

    async def run(self) -> None:
        while not self._stopping.is_set():
            delivery = self.store.claim()
            if delivery is None:
                try:
                    await asyncio.wait_for(self._stopping.wait(), timeout=self.idle_seconds)
                except TimeoutError:
                    continue
                continue
            try:
                await self.handler(delivery.payload, delivery.github_event)
            except Exception:
                LOGGER.exception("queued webhook processing failed", extra={"delivery": delivery.delivery_id})
                self.store.retry(delivery, self.max_attempts)
            else:
                self.store.complete(delivery.row_id)

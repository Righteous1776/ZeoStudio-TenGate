from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Callable, Iterator

from .capabilities import CapabilityPrism
from .errors import AuthorizationDenied, IntegrityFailure, InvalidTransition
from .github import VerifiedGitHubEvent
from .identity import SealVerifier


class DeliveryState(StrEnum):
    RECEIVED = "received"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class DeliveryView:
    delivery_id: str
    event_id: str
    event_name: str
    action: str | None
    repository: str
    subject: str
    resource_ref: str | None
    payload_sha256: str
    state: DeliveryState
    attempts: int
    worker: str | None
    lease_until: str | None
    result_ref: str | None
    last_error: str | None


class GitHubEventInbox:
    """Crash-recoverable, lease-based inbox for verified GitHub event metadata."""

    def __init__(
        self,
        database: str | Path,
        identity_gate: SealVerifier,
        *,
        max_attempts: int = 10,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if max_attempts < 1 or max_attempts > 1000:
            raise ValueError("max_attempts must be between 1 and 1000")
        self._db = sqlite3.connect(str(database), isolation_level=None, check_same_thread=False, timeout=5)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._max_attempts = max_attempts
        self._clock = clock or (lambda: datetime.now(UTC))
        self._identity_gate = identity_gate
        self._install()

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def capabilities(self) -> CapabilityPrism:
        return CapabilityPrism(fetch_pending=True, health=True)

    def health(self) -> dict[str, str]:
        return {"status": "ok", "storage": "sqlite", "delivery": "lease-based"}

    def _install(self) -> None:
        self._db.executescript(
            """
            PRAGMA journal_mode=WAL;
            PRAGMA busy_timeout=5000;
            CREATE TABLE IF NOT EXISTS tengate_component_versions (
              component TEXT PRIMARY KEY, version INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS github_event_inbox (
              delivery_id TEXT PRIMARY KEY,
              event_id TEXT UNIQUE NOT NULL,
              event_name TEXT NOT NULL,
              action TEXT,
              repository TEXT NOT NULL,
              subject TEXT NOT NULL,
              resource_ref TEXT,
              payload_sha256 TEXT NOT NULL,
              state TEXT NOT NULL CHECK(state IN ('received','processing','completed','failed')),
              attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts >= 0),
              worker TEXT,
              lease_until TEXT,
              result_ref TEXT,
              last_error TEXT,
              received_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS github_event_inbox_claim
              ON github_event_inbox(state, attempts, received_at, delivery_id);
            """
        )
        row = self._db.execute(
            "SELECT version FROM tengate_component_versions WHERE component='github_event_inbox'"
        ).fetchone()
        if row and row["version"] > 1:
            self._db.close()
            raise IntegrityFailure(f"GitHub inbox schema {row['version']} is newer than supported schema 1")
        if not row:
            self._db.execute(
                "INSERT INTO tengate_component_versions(component,version) VALUES('github_event_inbox',1)"
            )

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                yield
            except Exception:
                self._db.execute("ROLLBACK")
                raise
            else:
                self._db.execute("COMMIT")

    def _now(self) -> datetime:
        now = self._clock()
        if now.tzinfo is None:
            raise ValueError("clock must return a timezone-aware datetime")
        return now.astimezone(UTC)

    def register(self, event: VerifiedGitHubEvent) -> bool:
        if not self._identity_gate.verify(event.actor) or event.actor.subject != event.subject:
            raise AuthorizationDenied("event actor seal is invalid or does not match the verified subject")
        now = self._now().isoformat()
        immutable = (
            event.event_id,
            event.event_name,
            event.action,
            event.repository,
            event.subject,
            event.resource_ref,
            event.payload_sha256,
        )
        with self._transaction():
            row = self._db.execute(
                "SELECT event_id,event_name,action,repository,subject,resource_ref,payload_sha256 "
                "FROM github_event_inbox WHERE delivery_id=?",
                (event.delivery_id,),
            ).fetchone()
            if row:
                if tuple(row) != immutable:
                    raise IntegrityFailure("delivery id was reused with different verified metadata")
                return False
            self._db.execute(
                "INSERT INTO github_event_inbox("
                "delivery_id,event_id,event_name,action,repository,subject,resource_ref,payload_sha256,"
                "state,received_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (event.delivery_id, *immutable, DeliveryState.RECEIVED, now, now),
            )
        return True

    def claim(self, worker: str, *, lease_seconds: int = 60) -> DeliveryView | None:
        self._validate_worker(worker)
        if lease_seconds < 1 or lease_seconds > 86_400:
            raise ValueError("lease_seconds must be between 1 and 86400")
        now = self._now()
        now_text = now.isoformat()
        lease_until = (now + timedelta(seconds=lease_seconds)).isoformat()
        with self._transaction():
            self._db.execute(
                "UPDATE github_event_inbox SET state='failed',worker=NULL,lease_until=NULL,"
                "last_error=COALESCE(last_error,'attempt limit reached'),updated_at=? "
                "WHERE state='processing' AND lease_until<=? AND attempts>=?",
                (now_text, now_text, self._max_attempts),
            )
            row = self._db.execute(
                "SELECT delivery_id FROM github_event_inbox WHERE attempts<? AND "
                "(state IN ('received','failed') OR (state='processing' AND lease_until<=?)) "
                "ORDER BY received_at,delivery_id LIMIT 1",
                (self._max_attempts, now_text),
            ).fetchone()
            if not row:
                return None
            self._db.execute(
                "UPDATE github_event_inbox SET state='processing',attempts=attempts+1,worker=?,"
                "lease_until=?,last_error=NULL,updated_at=? WHERE delivery_id=?",
                (worker, lease_until, now_text, row["delivery_id"]),
            )
            claimed = self._db.execute(
                "SELECT * FROM github_event_inbox WHERE delivery_id=?", (row["delivery_id"],)
            ).fetchone()
        return self._view(claimed)

    def heartbeat(self, delivery_id: str, worker: str, *, lease_seconds: int = 60) -> DeliveryView:
        self._validate_worker(worker)
        if lease_seconds < 1 or lease_seconds > 86_400:
            raise ValueError("lease_seconds must be between 1 and 86400")
        now = self._now()
        with self._transaction():
            row = self._require_active(delivery_id, worker, now)
            lease_until = (now + timedelta(seconds=lease_seconds)).isoformat()
            self._db.execute(
                "UPDATE github_event_inbox SET lease_until=?,updated_at=? WHERE delivery_id=?",
                (lease_until, now.isoformat(), delivery_id),
            )
            updated = dict(row)
            updated["lease_until"] = lease_until
        return self._view(updated)

    def complete(self, delivery_id: str, worker: str, result_ref: str) -> bool:
        self._validate_worker(worker)
        if not result_ref or len(result_ref) > 2000:
            raise ValueError("result_ref must contain 1 to 2000 characters")
        now = self._now()
        with self._transaction():
            row = self._db.execute(
                "SELECT * FROM github_event_inbox WHERE delivery_id=?", (delivery_id,)
            ).fetchone()
            if not row:
                raise KeyError(delivery_id)
            if row["state"] == DeliveryState.COMPLETED and row["result_ref"] == result_ref:
                return False
            self._require_active_row(row, worker, now)
            self._db.execute(
                "UPDATE github_event_inbox SET state='completed',worker=NULL,lease_until=NULL,"
                "result_ref=?,last_error=NULL,updated_at=? WHERE delivery_id=?",
                (result_ref, now.isoformat(), delivery_id),
            )
        return True

    def fail(self, delivery_id: str, worker: str, error: str) -> None:
        self._validate_worker(worker)
        if not error or len(error) > 4000:
            raise ValueError("error must contain 1 to 4000 characters")
        now = self._now()
        with self._transaction():
            row = self._require_active(delivery_id, worker, now)
            self._db.execute(
                "UPDATE github_event_inbox SET state='failed',worker=NULL,lease_until=NULL,"
                "last_error=?,updated_at=? WHERE delivery_id=?",
                (error, now.isoformat(), row["delivery_id"]),
            )

    def get(self, delivery_id: str) -> DeliveryView:
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM github_event_inbox WHERE delivery_id=?", (delivery_id,)
            ).fetchone()
        if not row:
            raise KeyError(delivery_id)
        return self._view(row)

    def stats(self) -> dict[str, int]:
        counts = {state.value: 0 for state in DeliveryState}
        with self._lock:
            for row in self._db.execute(
                "SELECT state,COUNT(*) AS count FROM github_event_inbox GROUP BY state"
            ):
                counts[row["state"]] = row["count"]
        return counts

    def _require_active(self, delivery_id: str, worker: str, now: datetime) -> sqlite3.Row:
        row = self._db.execute(
            "SELECT * FROM github_event_inbox WHERE delivery_id=?", (delivery_id,)
        ).fetchone()
        if not row:
            raise KeyError(delivery_id)
        self._require_active_row(row, worker, now)
        return row

    @staticmethod
    def _require_active_row(row: sqlite3.Row, worker: str, now: datetime) -> None:
        if row["state"] != DeliveryState.PROCESSING:
            raise InvalidTransition(f"delivery is not processing: {row['state']}")
        if row["worker"] != worker:
            raise AuthorizationDenied("delivery lease belongs to another worker")
        if not row["lease_until"] or datetime.fromisoformat(row["lease_until"]) <= now:
            raise InvalidTransition("delivery lease expired")

    @staticmethod
    def _validate_worker(worker: str) -> None:
        if not worker or len(worker) > 200:
            raise ValueError("worker must contain 1 to 200 characters")

    @staticmethod
    def _view(row: sqlite3.Row | dict[str, object]) -> DeliveryView:
        return DeliveryView(
            delivery_id=str(row["delivery_id"]),
            event_id=str(row["event_id"]),
            event_name=str(row["event_name"]),
            action=str(row["action"]) if row["action"] is not None else None,
            repository=str(row["repository"]),
            subject=str(row["subject"]),
            resource_ref=str(row["resource_ref"]) if row["resource_ref"] is not None else None,
            payload_sha256=str(row["payload_sha256"]),
            state=DeliveryState(str(row["state"])),
            attempts=int(row["attempts"]),
            worker=str(row["worker"]) if row["worker"] is not None else None,
            lease_until=str(row["lease_until"]) if row["lease_until"] is not None else None,
            result_ref=str(row["result_ref"]) if row["result_ref"] is not None else None,
            last_error=str(row["last_error"]) if row["last_error"] is not None else None,
        )


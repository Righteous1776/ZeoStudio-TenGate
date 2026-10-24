from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Iterator, Mapping

from .errors import AuthorizationDenied, IntegrityFailure, InvalidTransition, RevisionConflict
from .identity import SealVerifier
from .integrity import canonical_json, digest, secret_findings
from .models import ActorSeal, InboxParcel, TaskState, TaskView


_TRANSITIONS: dict[str, tuple[TaskState, TaskState]] = {
    "assign": (TaskState.QUEUED, TaskState.ASSIGNED),
    "start": (TaskState.ASSIGNED, TaskState.RUNNING),
    "block": (TaskState.RUNNING, TaskState.BLOCKED),
    "resume": (TaskState.BLOCKED, TaskState.RUNNING),
    "submit_review": (TaskState.RUNNING, TaskState.REVIEW),
    "request_changes": (TaskState.REVIEW, TaskState.RUNNING),
    "accept": (TaskState.REVIEW, TaskState.ACCEPTED),
}


class GateKernel:
    """Durable coordination kernel with append-only, hash-linked events."""

    def __init__(self, database: str | Path, identity_gate: SealVerifier) -> None:
        self._db = sqlite3.connect(str(database), isolation_level=None, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._identity_gate = identity_gate
        self._install()

    def close(self) -> None:
        self._db.close()

    def _install(self) -> None:
        schema_version = self._db.execute("PRAGMA user_version").fetchone()[0]
        if schema_version > 1:
            self._db.close()
            raise IntegrityFailure(f"database schema {schema_version} is newer than supported schema 1")
        self._db.executescript(
            """
            PRAGMA foreign_keys=ON;
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS tasks (
              task_id TEXT PRIMARY KEY, state TEXT NOT NULL, revision INTEGER NOT NULL,
              assigned_lane TEXT, last_event_hash TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS task_events (
              sequence INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT UNIQUE NOT NULL,
              task_id TEXT NOT NULL, revision INTEGER NOT NULL, action TEXT NOT NULL,
              actor_subject TEXT NOT NULL, actor_issuer TEXT NOT NULL, occurred_at TEXT NOT NULL,
              details_json TEXT NOT NULL, previous_hash TEXT NOT NULL, event_hash TEXT UNIQUE NOT NULL,
              UNIQUE(task_id, revision), FOREIGN KEY(task_id) REFERENCES tasks(task_id) ON DELETE RESTRICT
            );
            CREATE TABLE IF NOT EXISTS evidence (
              evidence_id TEXT PRIMARY KEY, task_id TEXT NOT NULL, kind TEXT NOT NULL,
              immutable_ref TEXT NOT NULL, sha256 TEXT NOT NULL, verifier TEXT NOT NULL,
              created_at TEXT NOT NULL,
              FOREIGN KEY(task_id) REFERENCES tasks(task_id) ON DELETE RESTRICT
            );
            CREATE TABLE IF NOT EXISTS checkpoints (
              checkpoint_id TEXT PRIMARY KEY, task_id TEXT NOT NULL, revision INTEGER NOT NULL,
              summary TEXT NOT NULL, next_action TEXT NOT NULL, blocked_on TEXT,
              last_evidence TEXT, created_at TEXT NOT NULL,
              FOREIGN KEY(task_id) REFERENCES tasks(task_id) ON DELETE RESTRICT,
              FOREIGN KEY(last_evidence) REFERENCES evidence(evidence_id) ON DELETE RESTRICT
            );
            CREATE TABLE IF NOT EXISTS parcels (
              message_id TEXT NOT NULL, idempotency_key TEXT NOT NULL, recipient TEXT NOT NULL,
              task_id TEXT NOT NULL, sender_claim TEXT NOT NULL, verified_sender TEXT NOT NULL,
              kind TEXT NOT NULL, parcel_json TEXT NOT NULL, parcel_hash TEXT NOT NULL, received_at TEXT NOT NULL,
              acked_at TEXT, acked_by TEXT, PRIMARY KEY(message_id, recipient),
              UNIQUE(idempotency_key, recipient),
              FOREIGN KEY(task_id) REFERENCES tasks(task_id) ON DELETE RESTRICT
            );
            CREATE TABLE IF NOT EXISTS leases (
              lease_id TEXT PRIMARY KEY, task_id TEXT NOT NULL, path TEXT NOT NULL,
              lane TEXT NOT NULL, base_revision INTEGER NOT NULL, expires_at TEXT NOT NULL,
              FOREIGN KEY(task_id) REFERENCES tasks(task_id) ON DELETE RESTRICT
            );
            """
        )
        if schema_version == 0:
            self._db.execute("PRAGMA user_version=1")

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

    @staticmethod
    def _now() -> str:
        return datetime.now(UTC).isoformat()

    def _require_verified(self, actor: ActorSeal) -> None:
        if not self._identity_gate.verify(actor):
            raise AuthorizationDenied("actor seal is invalid, expired, or issued by an untrusted gate")

    def create_task(self, task_id: str, actor: ActorSeal, *, event_id: str | None = None) -> TaskView:
        self._require_verified(actor)
        if not task_id or len(task_id) > 200:
            raise ValueError("task_id must contain 1 to 200 characters")
        if not ({"owner", "governor"} & actor.roles):
            raise AuthorizationDenied("only owner/governor may create tasks")
        event_id = event_id or str(uuid.uuid4())
        with self._transaction():
            existing = self._db.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
            if existing:
                duplicate = self._db.execute("SELECT 1 FROM task_events WHERE event_id=?", (event_id,)).fetchone()
                if duplicate:
                    return self.get_task(task_id)
                raise RevisionConflict("task already exists")
            event_details = self._sealed_details({}, actor)
            occurred_at = self._now()
            event_hash = self._event_hash(
                event_id, task_id, 0, "create", actor.subject, actor.issuer,
                event_details, "0" * 64, occurred_at,
            )
            self._db.execute(
                "INSERT INTO tasks VALUES(?,?,?,?,?)",
                (task_id, TaskState.QUEUED, 0, None, event_hash),
            )
            self._insert_event(
                event_id, task_id, 0, "create", actor, event_details,
                "0" * 64, occurred_at, event_hash,
            )
        return self.get_task(task_id)

    def transition(
        self,
        task_id: str,
        action: str,
        actor: ActorSeal,
        *,
        expected_revision: int,
        event_id: str | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> TaskView:
        self._require_verified(actor)
        details = dict(details or {})
        event_id = event_id or str(uuid.uuid4())
        with self._transaction():
            duplicate = self._db.execute("SELECT task_id FROM task_events WHERE event_id=?", (event_id,)).fetchone()
            if duplicate:
                if duplicate["task_id"] != task_id:
                    raise IntegrityFailure("event_id reused across tasks")
                return self.get_task(task_id)
            row = self._db.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
            if not row:
                raise KeyError(task_id)
            if row["revision"] != expected_revision:
                raise RevisionConflict(f"expected revision {expected_revision}, actual {row['revision']}")
            current = TaskState(row["state"])
            if action not in _TRANSITIONS or _TRANSITIONS[action][0] != current:
                raise InvalidTransition(f"{action} is invalid from {current}")
            target = _TRANSITIONS[action][1]
            lane = self._authorize(action, actor, row, details)
            revision = expected_revision + 1
            previous_hash = row["last_event_hash"]
            event_details = self._sealed_details(details, actor)
            occurred_at = self._now()
            event_hash = self._event_hash(
                event_id, task_id, revision, action, actor.subject, actor.issuer,
                event_details, previous_hash, occurred_at,
            )
            self._insert_event(
                event_id, task_id, revision, action, actor, event_details,
                previous_hash, occurred_at, event_hash,
            )
            self._db.execute(
                "UPDATE tasks SET state=?, revision=?, assigned_lane=?, last_event_hash=? WHERE task_id=?",
                (target, revision, lane, event_hash, task_id),
            )
        return self.get_task(task_id)

    def _authorize(self, action: str, actor: ActorSeal, task: sqlite3.Row, details: Mapping[str, Any]) -> str | None:
        lane = task["assigned_lane"]
        if action == "assign":
            if not ({"owner", "governor"} & actor.roles):
                raise AuthorizationDenied("assignment requires owner/governor")
            lane = str(details.get("lane", ""))
            if not lane:
                raise ValueError("assignment requires lane")
        elif action in {"start", "block", "resume", "submit_review"}:
            if not actor.lane or actor.lane != lane:
                raise AuthorizationDenied("actor is outside the assigned lane")
            if action == "submit_review" and not self._has_evidence(task["task_id"]):
                raise IntegrityFailure("review requires immutable evidence")
        elif action == "request_changes":
            if not ({"owner", "governor", "reviewer"} & actor.roles):
                raise AuthorizationDenied("review decision requires reviewer authority")
        elif action == "accept":
            if "owner" not in actor.roles:
                raise AuthorizationDenied("acceptance requires verified owner")
            if not self._has_evidence(task["task_id"]):
                raise IntegrityFailure("acceptance requires immutable evidence")
        return lane

    def add_evidence(
        self, task_id: str, kind: str, immutable_ref: str, sha256: str, actor: ActorSeal
    ) -> str:
        self._require_verified(actor)
        self.get_task(task_id)
        if not kind or not immutable_ref:
            raise ValueError("evidence kind and immutable_ref are required")
        if len(sha256) != 64 or any(c not in "0123456789abcdef" for c in sha256.lower()):
            raise ValueError("sha256 must be 64 hexadecimal characters")
        created_at = self._now()
        verifier = f"{actor.issuer}:{actor.subject}"
        evidence_id = digest([task_id, kind, immutable_ref, sha256.lower(), verifier, created_at])
        with self._transaction():
            self._db.execute(
                "INSERT OR IGNORE INTO evidence VALUES(?,?,?,?,?,?,?)",
                (evidence_id, task_id, kind, immutable_ref, sha256.lower(), verifier, created_at),
            )
        return evidence_id

    def checkpoint(
        self, task_id: str, actor: ActorSeal, *, summary: str, next_action: str,
        blocked_on: str | None = None, last_evidence: str | None = None
    ) -> str:
        self._require_verified(actor)
        task = self.get_task(task_id)
        if actor.lane != task.assigned_lane and not ({"owner", "governor"} & actor.roles):
            raise AuthorizationDenied("checkpoint actor is outside assigned lane")
        created_at = self._now()
        checkpoint_id = digest([task_id, task.revision, summary, next_action, blocked_on, last_evidence, created_at])
        with self._transaction():
            self._db.execute(
                "INSERT OR IGNORE INTO checkpoints VALUES(?,?,?,?,?,?,?,?)",
                (checkpoint_id, task_id, task.revision, summary, next_action, blocked_on, last_evidence, created_at),
            )
        return checkpoint_id

    def receive(self, parcel: InboxParcel, transport_actor: ActorSeal) -> dict[str, Any]:
        parcel.validate()
        self._require_verified(transport_actor)
        self.get_task(parcel.task_id)
        findings = secret_findings(parcel.payload)
        if findings:
            raise IntegrityFailure("parcel payload resembles a secret")
        encoded = canonical_json({
            "schema_version": parcel.schema_version, "message_id": parcel.message_id,
            "idempotency_key": parcel.idempotency_key, "project_id": parcel.project_id,
            "task_id": parcel.task_id, "sender_claim": parcel.sender_claim,
            "recipients": parcel.recipients, "kind": parcel.kind,
            "created_at": parcel.created_at, "payload": parcel.payload,
            "requires_ack": parcel.requires_ack,
        })
        # Identity and creation time may change across transport retries. The
        # idempotency fingerprint covers the logical effect instead.
        parcel_hash = digest({
            "project_id": parcel.project_id, "task_id": parcel.task_id,
            "sender_claim": parcel.sender_claim, "recipients": parcel.recipients,
            "kind": parcel.kind, "payload": parcel.payload, "requires_ack": parcel.requires_ack,
        })
        results: list[dict[str, str | bool]] = []
        with self._transaction():
            for recipient in parcel.recipients:
                existing = self._db.execute(
                    "SELECT message_id,parcel_hash FROM parcels WHERE (message_id=? AND recipient=?) OR (idempotency_key=? AND recipient=?)",
                    (parcel.message_id, recipient, parcel.idempotency_key, recipient),
                ).fetchone()
                if existing:
                    if existing["parcel_hash"] != parcel_hash:
                        raise IntegrityFailure("message identity or idempotency key reused with different content")
                    results.append({"recipient": recipient, "duplicate": True, "message_id": existing["message_id"]})
                    continue
                self._db.execute(
                    "INSERT INTO parcels(message_id,idempotency_key,recipient,task_id,sender_claim,verified_sender,kind,parcel_json,parcel_hash,received_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (parcel.message_id, parcel.idempotency_key, recipient, parcel.task_id, parcel.sender_claim,
                     transport_actor.subject, parcel.kind, encoded, parcel_hash, self._now()),
                )
                results.append({"recipient": recipient, "duplicate": False, "message_id": parcel.message_id})
        return {"deliveries": results}

    def acknowledge(self, message_id: str, recipient: str, actor: ActorSeal) -> bool:
        self._require_verified(actor)
        if actor.subject != recipient and "governor" not in actor.roles:
            raise AuthorizationDenied("ack actor does not match recipient")
        with self._transaction():
            row = self._db.execute(
                "SELECT acked_at FROM parcels WHERE message_id=? AND recipient=?", (message_id, recipient)
            ).fetchone()
            if not row:
                raise KeyError(message_id)
            if row["acked_at"]:
                return False
            self._db.execute(
                "UPDATE parcels SET acked_at=?, acked_by=? WHERE message_id=? AND recipient=?",
                (self._now(), actor.subject, message_id, recipient),
            )
        return True

    def pending(self, recipient: str) -> list[dict[str, Any]]:
        rows = self._db.execute(
            "SELECT message_id,task_id,kind,received_at FROM parcels WHERE recipient=? AND acked_at IS NULL ORDER BY received_at,message_id",
            (recipient,),
        ).fetchall()
        return [dict(row) for row in rows]

    def acquire_lease(
        self, task_id: str, path: str, actor: ActorSeal, *, expected_revision: int, ttl_seconds: int = 300
    ) -> str:
        """Coordinate overlapping work; this never grants merge authority."""
        self._require_verified(actor)
        if ttl_seconds < 1 or ttl_seconds > 86_400:
            raise ValueError("ttl_seconds must be between 1 and 86400")
        if not path or path.startswith("/") or ".." in Path(path).parts:
            raise ValueError("lease path must be a repository-relative path")
        with self._transaction():
            task = self.get_task(task_id)
            if task.revision != expected_revision:
                raise RevisionConflict(f"expected revision {expected_revision}, actual {task.revision}")
            if not actor.lane or actor.lane != task.assigned_lane:
                raise AuthorizationDenied("lease actor is outside assigned lane")
            now = datetime.now(UTC)
            self._db.execute("DELETE FROM leases WHERE expires_at<=?", (now.isoformat(),))
            rows = self._db.execute("SELECT path FROM leases WHERE task_id=?", (task_id,)).fetchall()
            parts = Path(path).parts
            for row in rows:
                held = Path(row["path"]).parts
                shared = min(len(parts), len(held))
                if parts[:shared] == held[:shared]:
                    raise RevisionConflict(f"path overlaps an active lease: {row['path']}")
            lease_id = digest([task_id, path, actor.lane, expected_revision, now.isoformat()])
            self._db.execute(
                "INSERT INTO leases VALUES(?,?,?,?,?,?)",
                (lease_id, task_id, path, actor.lane, expected_revision,
                 (now + timedelta(seconds=ttl_seconds)).isoformat()),
            )
        return lease_id

    def validate_lease(self, lease_id: str, actor: ActorSeal) -> bool:
        self._require_verified(actor)
        row = self._db.execute("SELECT * FROM leases WHERE lease_id=?", (lease_id,)).fetchone()
        if not row or datetime.fromisoformat(row["expires_at"]) <= datetime.now(UTC):
            return False
        task = self.get_task(row["task_id"])
        return actor.lane == row["lane"] and task.revision == row["base_revision"]

    def release_lease(self, lease_id: str, actor: ActorSeal) -> bool:
        self._require_verified(actor)
        with self._transaction():
            row = self._db.execute("SELECT lane FROM leases WHERE lease_id=?", (lease_id,)).fetchone()
            if not row:
                return False
            if actor.lane != row["lane"] and not ({"owner", "governor"} & actor.roles):
                raise AuthorizationDenied("lease release actor is outside assigned lane")
            self._db.execute("DELETE FROM leases WHERE lease_id=?", (lease_id,))
        return True

    def get_task(self, task_id: str) -> TaskView:
        row = self._db.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        if not row:
            raise KeyError(task_id)
        return TaskView(task_id, TaskState(row["state"]), row["revision"], row["assigned_lane"], row["last_event_hash"])

    def verify_chain(self, task_id: str) -> bool:
        rows = self._db.execute("SELECT * FROM task_events WHERE task_id=? ORDER BY revision", (task_id,)).fetchall()
        previous = "0" * 64
        for expected_revision, row in enumerate(rows):
            if row["revision"] != expected_revision:
                return False
            details = json.loads(row["details_json"])
            expected = self._event_hash(
                row["event_id"], task_id, row["revision"], row["action"],
                row["actor_subject"], row["actor_issuer"], details, previous, row["occurred_at"],
            )
            if row["previous_hash"] != previous or row["event_hash"] != expected:
                return False
            previous = row["event_hash"]
        task = self.get_task(task_id)
        return bool(rows) and task.last_event_hash == previous

    def verify_integrity(self, task_id: str) -> bool:
        if not self.verify_chain(task_id):
            return False
        evidence_rows = self._db.execute(
            "SELECT * FROM evidence WHERE task_id=? ORDER BY evidence_id", (task_id,)
        ).fetchall()
        for row in evidence_rows:
            expected = digest([
                row["task_id"], row["kind"], row["immutable_ref"], row["sha256"],
                row["verifier"], row["created_at"],
            ])
            if row["evidence_id"] != expected:
                return False
        checkpoint_rows = self._db.execute(
            "SELECT * FROM checkpoints WHERE task_id=? ORDER BY checkpoint_id", (task_id,)
        ).fetchall()
        evidence_ids = {row["evidence_id"] for row in evidence_rows}
        for row in checkpoint_rows:
            expected = digest([
                row["task_id"], row["revision"], row["summary"], row["next_action"],
                row["blocked_on"], row["last_evidence"], row["created_at"],
            ])
            if row["checkpoint_id"] != expected:
                return False
            if row["last_evidence"] and row["last_evidence"] not in evidence_ids:
                return False
        if self.get_task(task_id).state in {TaskState.REVIEW, TaskState.ACCEPTED} and not evidence_rows:
            return False
        return True

    def _has_evidence(self, task_id: str) -> bool:
        return self._db.execute("SELECT 1 FROM evidence WHERE task_id=? LIMIT 1", (task_id,)).fetchone() is not None

    @staticmethod
    def _sealed_details(details: Mapping[str, Any], actor: ActorSeal) -> dict[str, Any]:
        return {
            "input": dict(details),
            "authorization": {"roles": sorted(actor.roles), "lane": actor.lane},
        }

    @staticmethod
    def _event_hash(event_id: str, task_id: str, revision: int, action: str,
                    actor_subject: str, actor_issuer: str, details: Mapping[str, Any],
                    previous_hash: str, occurred_at: str) -> str:
        return digest({"event_id": event_id, "task_id": task_id, "revision": revision, "action": action,
                       "actor": actor_subject, "issuer": actor_issuer, "details": details,
                       "previous_hash": previous_hash, "occurred_at": occurred_at})

    def _insert_event(self, event_id: str, task_id: str, revision: int, action: str, actor: ActorSeal,
                      details: Mapping[str, Any], previous_hash: str, occurred_at: str,
                      event_hash: str) -> None:
        self._db.execute(
            "INSERT INTO task_events(event_id,task_id,revision,action,actor_subject,actor_issuer,occurred_at,details_json,previous_hash,event_hash) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (event_id, task_id, revision, action, actor.subject, actor.issuer, occurred_at,
             canonical_json(details), previous_hash, event_hash),
        )


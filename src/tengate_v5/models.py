from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from datetime import datetime
from typing import Any, Mapping


class TaskState(StrEnum):
    QUEUED = "queued"
    ASSIGNED = "assigned"
    RUNNING = "running"
    BLOCKED = "blocked"
    REVIEW = "review"
    ACCEPTED = "accepted"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class ActorSeal:
    """Short-lived identity and authorization statement from a trusted gate."""

    subject: str
    roles: frozenset[str] = field(default_factory=frozenset)
    lane: str | None = None
    issuer: str = "unverified"
    issued_at: str = ""
    expires_at: str = ""
    nonce: str = ""
    proof: str = ""

    def unsigned_fields(self) -> dict[str, Any]:
        return {
            "subject": self.subject,
            "roles": sorted(self.roles),
            "lane": self.lane,
            "issuer": self.issuer,
            "issued_at": self.issued_at,
            "expires_at": self.expires_at,
            "nonce": self.nonce,
        }


@dataclass(frozen=True, slots=True)
class InboxParcel:
    schema_version: str
    message_id: str
    idempotency_key: str
    project_id: str
    task_id: str
    sender_claim: str
    recipients: tuple[str, ...]
    kind: str
    created_at: str
    payload: Mapping[str, Any]
    requires_ack: bool = True

    def validate(self) -> None:
        values = {
            "schema_version": self.schema_version,
            "message_id": self.message_id,
            "idempotency_key": self.idempotency_key,
            "project_id": self.project_id,
            "task_id": self.task_id,
            "sender_claim": self.sender_claim,
            "kind": self.kind,
            "created_at": self.created_at,
        }
        empty = [name for name, value in values.items() if not value]
        if empty or not self.recipients:
            raise ValueError(f"missing parcel fields: {', '.join(empty) or 'recipients'}")
        if self.schema_version != "1":
            raise ValueError(f"unsupported parcel schema: {self.schema_version}")
        if len(set(self.recipients)) != len(self.recipients):
            raise ValueError("recipients must be unique")
        try:
            created = datetime.fromisoformat(self.created_at.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("created_at must be ISO-8601") from exc
        if created.tzinfo is None:
            raise ValueError("created_at must include a timezone")


@dataclass(frozen=True, slots=True)
class TaskView:
    task_id: str
    state: TaskState
    revision: int
    assigned_lane: str | None
    last_event_hash: str


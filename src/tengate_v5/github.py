from __future__ import annotations

import hashlib
import hmac
import json
import re
from dataclasses import dataclass, field
from typing import Mapping

from .capabilities import CapabilityPrism
from .errors import WebhookRejected
from .identity import HmacSealAuthority
from .models import ActorSeal


_DELIVERY_ID = re.compile(r"^[A-Za-z0-9._:-]{1,200}$")
_EVENT_NAME = re.compile(r"^[a-z0-9_]{1,80}$")
_ACTION_NAME = re.compile(r"^[a-z0-9_]{1,80}$")


@dataclass(frozen=True, slots=True)
class GitHubBinding:
    roles: frozenset[str] = field(default_factory=frozenset)
    lane: str | None = None
    event_rules: frozenset[str] = field(default_factory=frozenset)
    seal_ttl_seconds: int = 300

    def permits(self, event_name: str, action: str | None) -> bool:
        action_name = action or "none"
        return f"{event_name}:*" in self.event_rules or f"{event_name}:{action_name}" in self.event_rules


@dataclass(frozen=True, slots=True)
class VerifiedGitHubEvent:
    delivery_id: str
    event_name: str
    action: str | None
    repository: str
    subject: str
    resource_ref: str | None
    payload_sha256: str
    actor: ActorSeal

    @property
    def event_id(self) -> str:
        return f"github:{self.delivery_id}"


class GitHubWebhookAdapter:
    """Verify inbound GitHub webhooks and apply repository-scoped actor policy."""

    def __init__(
        self,
        webhook_secret: bytes,
        seal_authority: HmacSealAuthority,
        bindings: Mapping[tuple[str, str], GitHubBinding],
        *,
        max_body_bytes: int = 1_048_576,
    ) -> None:
        if len(webhook_secret) < 32:
            raise ValueError("webhook secret must contain at least 32 bytes")
        if max_body_bytes < 1 or max_body_bytes > 10_485_760:
            raise ValueError("max_body_bytes must be between 1 and 10485760")
        normalized: dict[tuple[str, str], GitHubBinding] = {}
        for (repository, login), binding in bindings.items():
            key = (repository.casefold(), login.casefold())
            if not repository or not login or key in normalized:
                raise ValueError("bindings require unique repository/login pairs")
            if not binding.event_rules:
                raise ValueError("each binding requires at least one event rule")
            normalized[key] = binding
        self._webhook_secret = bytes(webhook_secret)
        self._seal_authority = seal_authority
        self._bindings = normalized
        self._max_body_bytes = max_body_bytes

    def capabilities(self) -> CapabilityPrism:
        return CapabilityPrism(verify_webhook=True, resolve_identity=True, health=True)

    def health(self) -> dict[str, str]:
        return {"status": "ok", "mode": "signed-webhook", "network": "not-required"}

    def verify(self, headers: Mapping[str, str], body: bytes) -> VerifiedGitHubEvent:
        if not isinstance(body, bytes):
            raise WebhookRejected("body must be raw bytes")
        if not body or len(body) > self._max_body_bytes:
            raise WebhookRejected("webhook body is empty or exceeds the configured limit")
        normalized_headers: dict[str, str] = {}
        for name, value in headers.items():
            if not isinstance(name, str) or not isinstance(value, str):
                raise WebhookRejected("webhook header names and values must be strings")
            key = name.casefold()
            if key in normalized_headers:
                raise WebhookRejected("duplicate case-insensitive webhook header")
            normalized_headers[key] = value.strip()
        content_type = normalized_headers.get("content-type", "")
        if not content_type.casefold().startswith("application/json"):
            raise WebhookRejected("content type must be application/json")
        delivery_id = normalized_headers.get("x-github-delivery", "")
        event_name = normalized_headers.get("x-github-event", "").casefold()
        signature = normalized_headers.get("x-hub-signature-256", "")
        if not _DELIVERY_ID.fullmatch(delivery_id):
            raise WebhookRejected("invalid GitHub delivery id")
        if not _EVENT_NAME.fullmatch(event_name):
            raise WebhookRejected("invalid GitHub event name")
        expected = "sha256=" + hmac.new(self._webhook_secret, body, hashlib.sha256).hexdigest()
        if len(signature) != len(expected) or not hmac.compare_digest(signature, expected):
            raise WebhookRejected("invalid GitHub webhook signature")
        try:
            payload = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
            raise WebhookRejected("webhook body is not valid UTF-8 JSON") from exc
        if not isinstance(payload, dict):
            raise WebhookRejected("webhook payload must be a JSON object")
        repository = self._nested_string(payload, "repository", "full_name")
        login = self._nested_string(payload, "sender", "login")
        action_value = payload.get("action")
        if action_value is not None and not isinstance(action_value, str):
            raise WebhookRejected("webhook action must be a string")
        action = action_value.casefold() if action_value else None
        if action is not None and not _ACTION_NAME.fullmatch(action):
            raise WebhookRejected("invalid webhook action")
        subject = f"github:{login.casefold()}"
        actor = self.resolve_actor(repository, subject, event_name, action)
        return VerifiedGitHubEvent(
            delivery_id=delivery_id,
            event_name=event_name,
            action=action,
            repository=repository,
            subject=subject,
            resource_ref=self._resource_ref(payload),
            payload_sha256=hashlib.sha256(body).hexdigest(),
            actor=actor,
        )

    def resolve_actor(
        self, repository: str, subject: str, event_name: str, action: str | None
    ) -> ActorSeal:
        """Re-evaluate current policy before processing a durable event."""
        if not subject.startswith("github:") or not subject.removeprefix("github:"):
            raise WebhookRejected("subject is not a GitHub identity")
        login = subject.removeprefix("github:")
        binding = self._bindings.get((repository.casefold(), login.casefold()))
        if not binding:
            raise WebhookRejected("sender is not bound to this repository")
        if not binding.permits(event_name, action):
            raise WebhookRejected("event/action is not permitted for this binding")
        return self._seal_authority.issue(
            f"github:{login.casefold()}",
            roles=binding.roles,
            lane=binding.lane,
            ttl_seconds=binding.seal_ttl_seconds,
        )

    @staticmethod
    def _nested_string(payload: Mapping[str, object], group: str, name: str) -> str:
        parent = payload.get(group)
        value = parent.get(name) if isinstance(parent, dict) else None
        if not isinstance(value, str) or not value:
            raise WebhookRejected(f"missing {group}.{name}")
        return value

    @staticmethod
    def _resource_ref(payload: Mapping[str, object]) -> str | None:
        for group in ("issue", "pull_request", "workflow_run", "check_run", "installation"):
            parent = payload.get(group)
            if not isinstance(parent, dict):
                continue
            field = "number" if group in {"issue", "pull_request"} else "id"
            value = parent.get(field)
            if isinstance(value, int) and value >= 0:
                return f"{group}:{value}"
        return None


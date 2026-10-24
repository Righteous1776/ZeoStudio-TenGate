from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from .errors import AuthorizationDenied, InvalidTransition
from .github import GitHubWebhookAdapter
from .github_inbox import DeliveryView, GitHubEventInbox
from .kernel import GateKernel


_TASK_PREFIX = re.compile(r"^[A-Z][A-Z0-9-]{1,30}$")


@dataclass(frozen=True, slots=True)
class BridgeResult:
    delivery_id: str
    task_id: str
    task_revision: int


class GitHubTaskBridge:
    """Convert approved GitHub resource events into idempotent queued tasks."""

    def __init__(
        self,
        adapter: GitHubWebhookAdapter,
        inbox: GitHubEventInbox,
        kernel: GateKernel,
        *,
        task_prefix: str = "TG-GH",
        routes: frozenset[str] = frozenset({"issues:opened", "pull_request:opened"}),
    ) -> None:
        if not _TASK_PREFIX.fullmatch(task_prefix):
            raise ValueError("task_prefix must be an uppercase identifier")
        if not routes:
            raise ValueError("at least one task route is required")
        self._adapter = adapter
        self._inbox = inbox
        self._kernel = kernel
        self._task_prefix = task_prefix
        self._routes = frozenset(routes)

    def process_one(self, worker: str, *, lease_seconds: int = 60) -> BridgeResult | None:
        delivery = self._inbox.claim(worker, lease_seconds=lease_seconds)
        if delivery is None:
            return None
        try:
            result = self.apply(delivery)
            self._inbox.complete(delivery.delivery_id, worker, f"task:{result.task_id}")
            return result
        except Exception as exc:
            try:
                self._inbox.fail(delivery.delivery_id, worker, f"{type(exc).__name__}: processing rejected")
            except (AuthorizationDenied, InvalidTransition):
                # A lost/expired lease belongs to the recovery path; preserve
                # the original processing failure for the caller.
                pass
            raise

    def apply(self, delivery: DeliveryView) -> BridgeResult:
        route = f"{delivery.event_name}:{delivery.action or 'none'}"
        if route not in self._routes:
            raise InvalidTransition(f"GitHub route is not configured for task creation: {route}")
        if not delivery.resource_ref:
            raise InvalidTransition("task-creating GitHub event requires a stable resource reference")
        actor = self._adapter.resolve_actor(
            delivery.repository, delivery.subject, delivery.event_name, delivery.action
        )
        task_id = self.task_id_for(delivery.repository, delivery.resource_ref)
        task = self._kernel.create_task(task_id, actor, event_id=delivery.event_id)
        return BridgeResult(delivery.delivery_id, task.task_id, task.revision)

    def task_id_for(self, repository: str, resource_ref: str) -> str:
        suffix = hashlib.sha256(f"{repository.casefold()}\0{resource_ref}".encode()).hexdigest()[:24].upper()
        return f"{self._task_prefix}-{suffix}"


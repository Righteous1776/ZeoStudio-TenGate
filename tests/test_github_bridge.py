from __future__ import annotations

import hashlib
import hmac
import json
import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta

from tengate_v5.errors import InvalidTransition, WebhookRejected
from tengate_v5.github import GitHubBinding, GitHubWebhookAdapter
from tengate_v5.github_bridge import GitHubTaskBridge
from tengate_v5.github_inbox import DeliveryState, GitHubEventInbox
from tengate_v5.identity import HmacSealAuthority
from tengate_v5.kernel import GateKernel


WEBHOOK_SECRET = b"github-bridge-webhook-secret-1234567890"
SEAL_KEY = b"github-bridge-seal-key-123456789012345"


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 10, 24, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: int) -> None:
        self.now += timedelta(seconds=seconds)


class GitHubTaskBridgeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = Clock()
        self.authority = HmacSealAuthority(SEAL_KEY, issuer="github-bridge-tests")
        self.adapter = self._adapter()
        self.inbox = GitHubEventInbox(":memory:", self.authority, clock=self.clock)
        self.kernel = GateKernel(":memory:", self.authority)
        self.bridge = GitHubTaskBridge(self.adapter, self.inbox, self.kernel)

    def tearDown(self) -> None:
        self.inbox.close()
        self.kernel.close()

    def _adapter(self, *, bindings: bool = True) -> GitHubWebhookAdapter:
        configured = {}
        if bindings:
            configured = {
                ("ZeoStudio/TenGate", "octo-owner"): GitHubBinding(
                    roles=frozenset({"governor"}),
                    lane="github-intake",
                    event_rules=frozenset({"issues:opened", "issues:closed"}),
                )
            }
        return GitHubWebhookAdapter(WEBHOOK_SECRET, self.authority, configured)

    def _event(self, *, action: str = "opened", delivery: str = "bridge-1"):
        payload = {
            "action": action,
            "repository": {"full_name": "ZeoStudio/TenGate"},
            "sender": {"login": "octo-owner"},
            "issue": {"number": 7},
        }
        body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        signature = hmac.new(WEBHOOK_SECRET, body, hashlib.sha256).hexdigest()
        return self.adapter.verify({
            "Content-Type": "application/json",
            "X-GitHub-Delivery": delivery,
            "X-GitHub-Event": "issues",
            "X-Hub-Signature-256": f"sha256={signature}",
        }, body)

    def test_verified_event_becomes_completed_queued_task(self) -> None:
        event = self._event()
        self.inbox.register(event)
        result = self.bridge.process_one("bridge-worker")
        self.assertIsNotNone(result)
        assert result is not None
        task = self.kernel.get_task(result.task_id)
        self.assertEqual((task.state, task.revision), ("queued", 0))
        delivery = self.inbox.get(event.delivery_id)
        self.assertEqual(delivery.state, DeliveryState.COMPLETED)
        self.assertEqual(delivery.result_ref, f"task:{result.task_id}")
        self.assertTrue(self.kernel.verify_integrity(result.task_id))

    def test_crash_after_task_creation_recovers_without_duplicate(self) -> None:
        event = self._event()
        self.inbox.register(event)
        claimed = self.inbox.claim("worker-before-crash", lease_seconds=5)
        assert claimed is not None
        first = self.bridge.apply(claimed)
        self.assertEqual(self.kernel.get_task(first.task_id).revision, 0)
        self.clock.advance(6)
        recovered = self.bridge.process_one("recovery-worker", lease_seconds=30)
        assert recovered is not None
        self.assertEqual(recovered.task_id, first.task_id)
        self.assertEqual(self.kernel.get_task(first.task_id).revision, 0)
        self.assertEqual(self.inbox.get(event.delivery_id).attempts, 2)
        self.assertEqual(self.inbox.get(event.delivery_id).state, DeliveryState.COMPLETED)

    def test_current_policy_is_rechecked_before_task_creation(self) -> None:
        event = self._event()
        self.inbox.register(event)
        revoked = GitHubTaskBridge(self._adapter(bindings=False), self.inbox, self.kernel)
        with self.assertRaises(WebhookRejected):
            revoked.process_one("bridge-worker")
        delivery = self.inbox.get(event.delivery_id)
        self.assertEqual(delivery.state, DeliveryState.FAILED)
        self.assertIsNone(self.kernel._db.execute("SELECT 1 FROM tasks").fetchone())

    def test_unconfigured_route_fails_closed(self) -> None:
        closed = self._event(action="closed", delivery="bridge-closed")
        self.inbox.register(closed)
        with self.assertRaises(InvalidTransition):
            self.bridge.process_one("bridge-worker")

    def test_missing_resource_fails_closed(self) -> None:
        missing = replace(self._event(delivery="bridge-missing"), resource_ref=None)
        self.inbox.register(missing)
        with self.assertRaises(InvalidTransition):
            self.bridge.process_one("bridge-worker")

    def test_task_identity_is_stable_across_repository_case(self) -> None:
        first = self.bridge.task_id_for("ZeoStudio/TenGate", "issue:7")
        second = self.bridge.task_id_for("zeostudio/tengate", "issue:7")
        self.assertEqual(first, second)
        self.assertTrue(first.startswith("TG-GH-"))


if __name__ == "__main__":
    unittest.main()


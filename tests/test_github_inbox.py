from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3
import tempfile
import threading
import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

from tengate_v5.errors import AuthorizationDenied, IntegrityFailure, InvalidTransition
from tengate_v5.github import GitHubBinding, GitHubWebhookAdapter, VerifiedGitHubEvent
from tengate_v5.github_inbox import DeliveryState, GitHubEventInbox
from tengate_v5.identity import HmacSealAuthority


WEBHOOK_SECRET = b"durable-github-inbox-webhook-secret-123456"
SEAL_KEY = b"durable-github-inbox-seal-key-1234567890"


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 10, 24, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: int) -> None:
        self.now += timedelta(seconds=seconds)


def verified_event(
    authority: HmacSealAuthority, *, delivery: str = "delivery-1", action: str = "opened"
) -> VerifiedGitHubEvent:
    adapter = GitHubWebhookAdapter(
        WEBHOOK_SECRET,
        authority,
        {
            ("ZeoStudio/TenGate", "octo-owner"): GitHubBinding(
                roles=frozenset({"governor"}),
                lane="github-intake",
                event_rules=frozenset({"issues:opened", "issues:closed"}),
            )
        },
    )
    payload = {
        "action": action,
        "repository": {"full_name": "ZeoStudio/TenGate"},
        "sender": {"login": "octo-owner"},
        "issue": {"number": 42},
    }
    body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    signature = hmac.new(WEBHOOK_SECRET, body, hashlib.sha256).hexdigest()
    headers = {
        "Content-Type": "application/json",
        "X-GitHub-Delivery": delivery,
        "X-GitHub-Event": "issues",
        "X-Hub-Signature-256": f"sha256={signature}",
    }
    return adapter.verify(headers, body)


class GitHubEventInboxTests(unittest.TestCase):
    def setUp(self) -> None:
        self.authority = HmacSealAuthority(SEAL_KEY, issuer="github-inbox-tests")
        self.clock = Clock()
        self.inbox = GitHubEventInbox(":memory:", self.authority, clock=self.clock)

    def tearDown(self) -> None:
        self.inbox.close()

    def test_register_is_idempotent_and_rejects_identity_reuse(self) -> None:
        event = verified_event(self.authority)
        self.assertTrue(self.inbox.register(event))
        self.assertFalse(self.inbox.register(event))
        changed = replace(event, payload_sha256="f" * 64)
        with self.assertRaises(IntegrityFailure):
            self.inbox.register(changed)
        forged = replace(event, subject="github:other")
        with self.assertRaises(AuthorizationDenied):
            self.inbox.register(forged)

    def test_receive_process_heartbeat_complete(self) -> None:
        event = verified_event(self.authority)
        self.inbox.register(event)
        claimed = self.inbox.claim("worker-a", lease_seconds=30)
        self.assertIsNotNone(claimed)
        assert claimed is not None
        self.assertEqual((claimed.state, claimed.attempts), (DeliveryState.PROCESSING, 1))
        first_lease = claimed.lease_until
        self.clock.advance(5)
        renewed = self.inbox.heartbeat(event.delivery_id, "worker-a", lease_seconds=60)
        self.assertGreater(renewed.lease_until or "", first_lease or "")
        with self.assertRaises(AuthorizationDenied):
            self.inbox.complete(event.delivery_id, "worker-b", "task:TG-1")
        self.assertTrue(self.inbox.complete(event.delivery_id, "worker-a", "task:TG-1"))
        self.assertFalse(self.inbox.complete(event.delivery_id, "worker-a", "task:TG-1"))
        self.assertEqual(self.inbox.get(event.delivery_id).state, DeliveryState.COMPLETED)
        self.assertIsNone(self.inbox.claim("worker-b"))

    def test_failure_is_retried_with_incremented_attempt(self) -> None:
        event = verified_event(self.authority)
        self.inbox.register(event)
        first = self.inbox.claim("worker-a")
        assert first is not None
        self.inbox.fail(event.delivery_id, "worker-a", "temporary failure")
        self.assertEqual(self.inbox.get(event.delivery_id).state, DeliveryState.FAILED)
        second = self.inbox.claim("worker-b")
        assert second is not None
        self.assertEqual(second.attempts, 2)
        self.inbox.complete(event.delivery_id, "worker-b", "task:TG-2")

    def test_expired_lease_is_recovered_after_reopen(self) -> None:
        self.inbox.close()
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "inbox.db"
            first = GitHubEventInbox(db, self.authority, clock=self.clock)
            event = verified_event(self.authority)
            first.register(event)
            first_claim = first.claim("worker-a", lease_seconds=10)
            assert first_claim is not None
            first.close()
            self.clock.advance(11)
            recovered = GitHubEventInbox(db, self.authority, clock=self.clock)
            second_claim = recovered.claim("worker-b", lease_seconds=10)
            assert second_claim is not None
            self.assertEqual(second_claim.attempts, 2)
            with self.assertRaises(AuthorizationDenied):
                recovered.complete(event.delivery_id, "worker-a", "task:stale")
            recovered.complete(event.delivery_id, "worker-b", "task:recovered")
            recovered.close()
        self.inbox = GitHubEventInbox(":memory:", self.authority, clock=self.clock)

    def test_two_connections_claim_only_once(self) -> None:
        self.inbox.close()
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "claim.db"
            first = GitHubEventInbox(db, self.authority, clock=self.clock)
            second = GitHubEventInbox(db, self.authority, clock=self.clock)
            first.register(verified_event(self.authority))
            barrier = threading.Barrier(2)
            results: list[str | None] = []

            def claim(inbox: GitHubEventInbox, worker: str) -> None:
                barrier.wait()
                item = inbox.claim(worker)
                results.append(item.worker if item else None)

            threads = [
                threading.Thread(target=claim, args=(first, "worker-a")),
                threading.Thread(target=claim, args=(second, "worker-b")),
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            self.assertEqual(sum(result is not None for result in results), 1)
            first.close()
            second.close()
        self.inbox = GitHubEventInbox(":memory:", self.authority, clock=self.clock)

    def test_attempt_limit_stops_retry_loop(self) -> None:
        self.inbox.close()
        self.inbox = GitHubEventInbox(":memory:", self.authority, clock=self.clock, max_attempts=2)
        event = verified_event(self.authority)
        self.inbox.register(event)
        for worker in ("worker-a", "worker-b"):
            item = self.inbox.claim(worker)
            assert item is not None
            self.inbox.fail(event.delivery_id, worker, "retry")
        self.assertIsNone(self.inbox.claim("worker-c"))
        self.assertEqual(self.inbox.get(event.delivery_id).attempts, 2)

    def test_expired_worker_cannot_finish_without_reclaim(self) -> None:
        event = verified_event(self.authority)
        self.inbox.register(event)
        self.inbox.claim("worker-a", lease_seconds=5)
        self.clock.advance(6)
        with self.assertRaises(InvalidTransition):
            self.inbox.complete(event.delivery_id, "worker-a", "task:late")

    def test_newer_component_schema_is_rejected(self) -> None:
        self.inbox.close()
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "future.db"
            initial = GitHubEventInbox(db, self.authority, clock=self.clock)
            initial.close()
            connection = sqlite3.connect(db)
            connection.execute(
                "UPDATE tengate_component_versions SET version=99 WHERE component='github_event_inbox'"
            )
            connection.commit()
            connection.close()
            with self.assertRaises(IntegrityFailure):
                GitHubEventInbox(db, self.authority, clock=self.clock)
        self.inbox = GitHubEventInbox(":memory:", self.authority, clock=self.clock)


if __name__ == "__main__":
    unittest.main()


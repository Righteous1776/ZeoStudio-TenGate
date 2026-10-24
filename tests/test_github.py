from __future__ import annotations

import hashlib
import hmac
import json
import unittest

from tengate_v5.errors import WebhookRejected
from tengate_v5.github import GitHubBinding, GitHubWebhookAdapter
from tengate_v5.identity import HmacSealAuthority
from tengate_v5.kernel import GateKernel


WEBHOOK_SECRET = b"github-webhook-secret-for-offline-security-tests"
SEAL_KEY = b"github-seal-key-for-offline-security-tests-123"


def encode(payload: object) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def headers(body: bytes, *, delivery: str = "delivery-1", event: str = "issues") -> dict[str, str]:
    signature = hmac.new(WEBHOOK_SECRET, body, hashlib.sha256).hexdigest()
    return {
        "Content-Type": "application/json; charset=utf-8",
        "X-GitHub-Delivery": delivery,
        "X-GitHub-Event": event,
        "X-Hub-Signature-256": f"sha256={signature}",
    }


class GitHubWebhookTests(unittest.TestCase):
    def setUp(self) -> None:
        self.authority = HmacSealAuthority(SEAL_KEY, issuer="github-gateway")
        self.adapter = GitHubWebhookAdapter(
            WEBHOOK_SECRET,
            self.authority,
            {
                ("ZeoStudio/TenGate", "octo-owner"): GitHubBinding(
                    roles=frozenset({"governor"}),
                    lane="github-intake",
                    event_rules=frozenset({"issues:opened", "issue_comment:created"}),
                )
            },
        )
        self.payload = {
            "action": "opened",
            "repository": {"full_name": "ZeoStudio/TenGate"},
            "sender": {"login": "octo-owner"},
            "issue": {"number": 42},
        }

    def test_valid_webhook_mints_scoped_seal_and_replays_idempotently(self) -> None:
        body = encode(self.payload)
        event = self.adapter.verify(headers(body), body)
        self.assertEqual(event.subject, "github:octo-owner")
        self.assertEqual(event.repository, "ZeoStudio/TenGate")
        self.assertEqual(event.resource_ref, "issue:42")
        self.assertEqual(event.event_id, "github:delivery-1")
        self.assertEqual(event.actor.roles, frozenset({"governor"}))
        self.assertEqual(event.actor.lane, "github-intake")
        self.assertTrue(self.authority.verify(event.actor))
        kernel = GateKernel(":memory:", self.authority)
        first = kernel.create_task("TG-GH", event.actor, event_id=event.event_id)
        replay = self.adapter.verify(headers(body), body)
        second = kernel.create_task("TG-GH", replay.actor, event_id=replay.event_id)
        self.assertEqual(first, second)
        self.assertEqual(second.revision, 0)
        kernel.close()

    def test_payload_cannot_grant_roles(self) -> None:
        payload = dict(self.payload)
        payload["roles"] = ["owner", "governor"]
        payload["lane"] = "release"
        body = encode(payload)
        event = self.adapter.verify(headers(body), body)
        self.assertEqual(event.actor.roles, frozenset({"governor"}))
        self.assertEqual(event.actor.lane, "github-intake")

    def test_body_tampering_breaks_signature(self) -> None:
        body = encode(self.payload)
        signed_headers = headers(body)
        changed = encode({**self.payload, "action": "closed"})
        with self.assertRaises(WebhookRejected):
            self.adapter.verify(signed_headers, changed)

    def test_repository_sender_and_event_rules_are_enforced(self) -> None:
        for mutation, event_name in (
            ({"repository": {"full_name": "Other/Repo"}}, "issues"),
            ({"sender": {"login": "unknown"}}, "issues"),
            ({"action": "closed"}, "issues"),
            ({}, "pull_request_target"),
        ):
            payload = {**self.payload, **mutation}
            body = encode(payload)
            with self.subTest(mutation=mutation, event=event_name):
                with self.assertRaises(WebhookRejected):
                    self.adapter.verify(headers(body, event=event_name), body)

    def test_transport_limits_and_required_headers(self) -> None:
        body = encode(self.payload)
        cases = [
            ({**headers(body), "Content-Type": "text/plain"}, body),
            ({name: value for name, value in headers(body).items() if name != "X-GitHub-Delivery"}, body),
            ({**headers(body), "X-Hub-Signature-256": "sha256=" + "0" * 64}, body),
            ({**headers(body), "x-github-event": "issues"}, body),
            (headers(b"not-json"), b"not-json"),
        ]
        for event_headers, event_body in cases:
            with self.subTest(headers=event_headers):
                with self.assertRaises(WebhookRejected):
                    self.adapter.verify(event_headers, event_body)
        limited = GitHubWebhookAdapter(
            WEBHOOK_SECRET,
            self.authority,
            {("ZeoStudio/TenGate", "octo-owner"): GitHubBinding(event_rules=frozenset({"issues:*"}))},
            max_body_bytes=16,
        )
        with self.assertRaises(WebhookRejected):
            limited.verify(headers(body), body)
        invalid_action = encode({**self.payload, "action": "opened now"})
        with self.assertRaises(WebhookRejected):
            self.adapter.verify(headers(invalid_action), invalid_action)

    def test_capabilities_and_health_disclose_offline_scope(self) -> None:
        capabilities = self.adapter.capabilities()
        self.assertTrue(capabilities.verify_webhook)
        self.assertTrue(capabilities.resolve_identity)
        self.assertFalse(capabilities.dispatch)
        self.assertEqual(self.adapter.health()["network"], "not-required")
        self.assertNotIn(WEBHOOK_SECRET.decode("ascii"), repr(self.adapter))


if __name__ == "__main__":
    unittest.main()


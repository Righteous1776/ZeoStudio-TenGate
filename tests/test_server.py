from __future__ import annotations

import hashlib
import hmac
import http.client
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path

from tengate_v5.config import load_config
from tengate_v5.runtime import RuntimeStack
from tengate_v5.server import create_server
from tengate_v5.service import BridgeWorker


SEAL_SECRET = "server-seal-secret-value-123456789012"
WEBHOOK_SECRET = "server-webhook-secret-value-123456789"


class ServerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        config_path = root / "config.json"
        config_path.write_text(json.dumps({
            "schema_version": "1",
            "database": "server.db",
            "identity": {"issuer": "server-tests", "seal_key_env": "SERVER_SEAL_KEY"},
            "github": {
                "webhook_secret_env": "SERVER_WEBHOOK_SECRET",
                "max_body_bytes": 4096,
                "max_attempts": 3,
                "bindings": [{
                    "repository": "ZeoStudio/TenGate",
                    "login": "octo-owner",
                    "roles": ["governor"],
                    "lane": "github-intake",
                    "event_rules": ["issues:opened"],
                    "seal_ttl_seconds": 300,
                }],
            },
            "worker": {"name": "server-worker", "lease_seconds": 60},
            "bridge": {"task_prefix": "TG-GH", "routes": ["issues:opened"]},
        }), encoding="utf-8")
        config = load_config(config_path)
        self.runtime = RuntimeStack.open(config, {
            "SERVER_SEAL_KEY": SEAL_SECRET,
            "SERVER_WEBHOOK_SECRET": WEBHOOK_SECRET,
        })
        self.server = create_server(self.runtime, "127.0.0.1", 0)
        self.worker: BridgeWorker | None = None
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        if self.worker is not None:
            self.worker.stop()
        self.runtime.close()
        self.tmp.cleanup()

    def request(
        self, method: str, path: str, body: bytes | None = None, headers: dict[str, str] | None = None
    ) -> tuple[int, dict[str, object], dict[str, str]]:
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=3)
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        payload = json.loads(response.read())
        response_headers = dict(response.getheaders())
        connection.close()
        return response.status, payload, response_headers

    def signed_request(self, delivery_id: str, body: bytes) -> tuple[int, dict[str, object], dict[str, str]]:
        signature = hmac.new(WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()
        return self.request("POST", "/github/webhook", body, {
            "Content-Type": "application/json",
            "X-GitHub-Delivery": delivery_id,
            "X-GitHub-Event": "issues",
            "X-Hub-Signature-256": f"sha256={signature}",
        })

    def test_health_endpoint_is_minimal_and_not_cached(self) -> None:
        status, payload, headers = self.request("GET", "/healthz")
        self.assertEqual(status, 200)
        self.assertEqual(payload["service"], "tengate-v5")
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertNotIn(SEAL_SECRET, json.dumps(payload))
        self.assertNotIn(WEBHOOK_SECRET, json.dumps(payload))

    def test_intake_only_readiness_is_explicit(self) -> None:
        status, payload, _ = self.request("GET", "/readyz")
        self.assertEqual((status, payload["status"]), (200, "ready"))
        self.assertEqual(payload["mode"], "intake-only")

    def test_signed_webhook_is_durably_deduplicated(self) -> None:
        body = json.dumps({
            "action": "opened",
            "repository": {"full_name": "ZeoStudio/TenGate"},
            "sender": {"login": "octo-owner"},
            "issue": {"number": 7},
        }, sort_keys=True, separators=(",", ":")).encode()
        status, first, _ = self.signed_request("server-delivery-1", body)
        self.assertEqual((status, first["registered"]), (202, True))
        status, replay, _ = self.signed_request("server-delivery-1", body)
        self.assertEqual((status, replay["registered"]), (202, False))
        self.assertEqual(self.runtime.inbox.stats()["received"], 1)

    def test_bad_signature_and_oversized_body_are_rejected(self) -> None:
        body = b"{}"
        status, payload, _ = self.request("POST", "/github/webhook", body, {
            "Content-Type": "application/json",
            "X-GitHub-Delivery": "bad-signature",
            "X-GitHub-Event": "issues",
            "X-Hub-Signature-256": "sha256=" + "0" * 64,
        })
        self.assertEqual((status, payload["status"]), (401, "rejected"))
        status, payload, _ = self.request("POST", "/github/webhook", b"x" * 4097, {
            "Content-Type": "application/json",
            "X-GitHub-Delivery": "too-large",
            "X-GitHub-Event": "issues",
            "X-Hub-Signature-256": "sha256=" + "0" * 64,
        })
        self.assertEqual((status, payload["status"]), (413, "rejected"))

    def test_unknown_route_returns_json_404(self) -> None:
        status, payload, _ = self.request("GET", "/missing")
        self.assertEqual((status, payload["status"]), (404, "not_found"))

    def test_http_receive_is_automatically_processed_with_worker(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        self.worker = BridgeWorker(
            self.runtime.bridge,
            self.runtime.config.worker.name,
            lease_seconds=self.runtime.config.worker.lease_seconds,
            poll_interval_ms=10,
        )
        self.server = create_server(self.runtime, "127.0.0.1", 0, worker=self.worker)
        self.worker.start()
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        status, ready, _ = self.request("GET", "/readyz")
        self.assertEqual((status, ready["status"]), (200, "ready"))
        body = json.dumps({
            "action": "opened",
            "repository": {"full_name": "ZeoStudio/TenGate"},
            "sender": {"login": "octo-owner"},
            "issue": {"number": 8},
        }, sort_keys=True, separators=(",", ":")).encode()
        status, received, _ = self.signed_request("server-auto-1", body)
        self.assertEqual((status, received["registered"]), (202, True))
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and (
            self.runtime.inbox.stats()["completed"] != 1
            or self.worker.snapshot().processed != 1
        ):
            time.sleep(0.01)
        self.assertEqual(self.runtime.inbox.stats()["completed"], 1)
        status, health, _ = self.request("GET", "/healthz")
        self.assertEqual(health["worker"]["processed"], 1)


if __name__ == "__main__":
    unittest.main()

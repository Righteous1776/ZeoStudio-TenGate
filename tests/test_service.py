from __future__ import annotations

import hashlib
import hmac
import json
import tempfile
import time
import unittest
from pathlib import Path

from tengate_v5.config import load_config
from tengate_v5.github_inbox import DeliveryState
from tengate_v5.runtime import RuntimeStack
from tengate_v5.server import WebhookApplication
from tengate_v5.service import BridgeWorker, WorkerState


SEAL_SECRET = "service-seal-secret-value-12345678901"
WEBHOOK_SECRET = "service-webhook-secret-value-12345678"


class ServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        config_path = root / "config.json"
        config_path.write_text(json.dumps({
            "schema_version": "1",
            "database": "service.db",
            "identity": {"issuer": "service-tests", "seal_key_env": "SERVICE_SEAL_KEY"},
            "github": {
                "webhook_secret_env": "SERVICE_WEBHOOK_SECRET",
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
            "worker": {"name": "service-worker", "lease_seconds": 60, "poll_interval_ms": 10},
            "bridge": {"task_prefix": "TG-GH", "routes": ["issues:opened"]},
        }), encoding="utf-8")
        self.config = load_config(config_path)
        self.runtime = RuntimeStack.open(self.config, {
            "SERVICE_SEAL_KEY": SEAL_SECRET,
            "SERVICE_WEBHOOK_SECRET": WEBHOOK_SECRET,
        })
        self.worker: BridgeWorker | None = None

    def tearDown(self) -> None:
        if self.worker is not None:
            self.worker.stop()
        self.runtime.close()
        self.tmp.cleanup()

    def register(self, delivery_id: str, *, resource: bool = True) -> None:
        payload: dict[str, object] = {
            "action": "opened",
            "repository": {"full_name": "ZeoStudio/TenGate"},
            "sender": {"login": "octo-owner"},
        }
        if resource:
            payload["issue"] = {"number": 42}
        body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        signature = hmac.new(WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()
        event = self.runtime.adapter.verify({
            "Content-Type": "application/json",
            "X-GitHub-Delivery": delivery_id,
            "X-GitHub-Event": "issues",
            "X-Hub-Signature-256": f"sha256={signature}",
        }, body)
        self.assertTrue(self.runtime.inbox.register(event))

    def new_worker(self) -> BridgeWorker:
        self.worker = BridgeWorker(
            self.runtime.bridge,
            self.config.worker.name,
            lease_seconds=self.config.worker.lease_seconds,
            poll_interval_ms=self.config.worker.poll_interval_ms,
        )
        return self.worker

    def wait_until(self, condition, timeout: float = 2.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if condition():
                return
            time.sleep(0.01)
        self.fail("condition was not met before timeout")

    def test_worker_processes_event_and_stops_cleanly(self) -> None:
        self.register("worker-delivery-1")
        worker = self.new_worker()
        self.assertTrue(worker.start())
        self.assertFalse(worker.start())
        self.wait_until(lambda: self.runtime.inbox.stats()["completed"] == 1)
        snapshot = worker.snapshot()
        self.assertEqual(snapshot.state, WorkerState.RUNNING)
        self.assertEqual((snapshot.processed, snapshot.errors), (1, 0))
        delivery = self.runtime.inbox.get("worker-delivery-1")
        self.assertTrue(delivery.result_ref and delivery.result_ref.startswith("task:"))
        self.assertTrue(self.runtime.kernel.verify_integrity(delivery.result_ref.removeprefix("task:")))
        self.assertTrue(worker.stop())
        self.assertEqual(worker.snapshot().state, WorkerState.STOPPED)

    def test_worker_retries_poison_event_to_configured_ceiling(self) -> None:
        self.register("worker-poison-1", resource=False)
        worker = self.new_worker()
        worker.start()
        self.wait_until(lambda: self.runtime.inbox.get("worker-poison-1").attempts == 3)
        delivery = self.runtime.inbox.get("worker-poison-1")
        self.assertEqual(delivery.state, DeliveryState.FAILED)
        self.wait_until(lambda: worker.snapshot().errors == 3)
        self.assertEqual(worker.snapshot().last_error_type, "InvalidTransition")

    def test_readiness_tracks_worker_lifecycle(self) -> None:
        worker = self.new_worker()
        app = WebhookApplication(self.runtime, worker)
        status, payload = app.ready()
        self.assertEqual((status, payload["status"]), (503, "not_ready"))
        worker.start()
        status, payload = app.ready()
        self.assertEqual((status, payload["status"]), (200, "ready"))
        worker.stop()
        status, payload = app.ready()
        self.assertEqual((status, payload["status"]), (503, "not_ready"))

    def test_optional_poll_interval_defaults_and_is_bounded(self) -> None:
        document = json.loads((Path(self.tmp.name) / "config.json").read_text(encoding="utf-8"))
        del document["worker"]["poll_interval_ms"]
        fallback = Path(self.tmp.name) / "fallback.json"
        fallback.write_text(json.dumps(document), encoding="utf-8")
        self.assertEqual(load_config(fallback).worker.poll_interval_ms, 250)
        document["worker"]["poll_interval_ms"] = 1
        fallback.write_text(json.dumps(document), encoding="utf-8")
        with self.assertRaises(ValueError):
            load_config(fallback)


if __name__ == "__main__":
    unittest.main()

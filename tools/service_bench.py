from __future__ import annotations

import hashlib
import hmac
import json
import tempfile
import time
from pathlib import Path

from tengate_v5.config import (
    BindingConfig,
    BridgeConfig,
    GitHubConfig,
    IdentityConfig,
    TenGateConfig,
    WorkerConfig,
)
from tengate_v5.runtime import RuntimeStack
from tengate_v5.service import BridgeWorker


DELIVERIES = 1000
SEAL_SECRET = "bench-seal-secret-value-1234567890123"
WEBHOOK_SECRET = "bench-webhook-secret-value-1234567890"


def main() -> None:
    with tempfile.TemporaryDirectory() as directory:
        config = TenGateConfig(
            schema_version="1",
            database=Path(directory) / "service-bench.db",
            identity=IdentityConfig(issuer="service-bench", seal_key_env="BENCH_SEAL_KEY"),
            github=GitHubConfig(
                webhook_secret_env="BENCH_WEBHOOK_SECRET",
                max_body_bytes=65_536,
                max_attempts=3,
                bindings=(BindingConfig(
                    repository="ZeoStudio/TenGate",
                    login="bench-owner",
                    roles=frozenset({"governor"}),
                    lane="github-intake",
                    event_rules=frozenset({"issues:opened"}),
                    seal_ttl_seconds=300,
                ),),
            ),
            worker=WorkerConfig(name="bench-worker", lease_seconds=60, poll_interval_ms=10),
            bridge=BridgeConfig(task_prefix="TG-GH", routes=frozenset({"issues:opened"})),
        )
        with RuntimeStack.open(config, {
            "BENCH_SEAL_KEY": SEAL_SECRET,
            "BENCH_WEBHOOK_SECRET": WEBHOOK_SECRET,
        }) as runtime:
            for number in range(DELIVERIES):
                body = json.dumps({
                    "action": "opened",
                    "repository": {"full_name": "ZeoStudio/TenGate"},
                    "sender": {"login": "bench-owner"},
                    "issue": {"number": number},
                }, sort_keys=True, separators=(",", ":")).encode()
                signature = hmac.new(WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()
                event = runtime.adapter.verify({
                    "Content-Type": "application/json",
                    "X-GitHub-Delivery": f"service-bench-{number}",
                    "X-GitHub-Event": "issues",
                    "X-Hub-Signature-256": f"sha256={signature}",
                }, body)
                if not runtime.inbox.register(event):
                    raise RuntimeError("unexpected duplicate delivery")

            worker = BridgeWorker(
                runtime.bridge,
                config.worker.name,
                lease_seconds=config.worker.lease_seconds,
                poll_interval_ms=config.worker.poll_interval_ms,
            )
            started = time.perf_counter()
            worker.start()
            deadline = started + 30
            while worker.snapshot().processed < DELIVERIES and time.perf_counter() < deadline:
                time.sleep(0.001)
            finished = time.perf_counter()
            if not worker.stop():
                raise RuntimeError("worker did not stop")
            snapshot = worker.snapshot()
            stats = runtime.inbox.stats()
            if snapshot.processed != DELIVERIES or snapshot.errors or stats["completed"] != DELIVERIES:
                raise RuntimeError("service benchmark did not complete exactly once")
            elapsed = finished - started
            print(json.dumps({
                "deliveries": DELIVERIES,
                "elapsed_ms": round(elapsed * 1000, 4),
                "errors": snapshot.errors,
                "processed": snapshot.processed,
                "remaining": stats["received"] + stats["processing"] + stats["failed"],
                "schema": "tengate.service-bench.v1",
                "throughput_per_second": round(DELIVERIES / elapsed, 2),
            }, sort_keys=True))


if __name__ == "__main__":
    main()

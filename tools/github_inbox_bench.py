#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import hmac
import json
import statistics
import tempfile
import time
from pathlib import Path

from tengate_v5.github import GitHubBinding, GitHubWebhookAdapter
from tengate_v5.github_bridge import GitHubTaskBridge
from tengate_v5.github_inbox import GitHubEventInbox
from tengate_v5.identity import HmacSealAuthority
from tengate_v5.kernel import GateKernel


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(len(ordered) * fraction))]


def main() -> None:
    webhook_secret = b"github-inbox-bench-webhook-secret-123456"
    authority = HmacSealAuthority(b"github-inbox-bench-seal-key-123456789", issuer="bench")
    adapter = GitHubWebhookAdapter(
        webhook_secret,
        authority,
        {
            ("ZeoStudio/TenGate", "bench-user"): GitHubBinding(
                roles=frozenset({"governor"}),
                lane="bench",
                event_rules=frozenset({"issues:opened"}),
            )
        },
    )
    register_ms: list[float] = []
    process_ms: list[float] = []
    with tempfile.TemporaryDirectory() as tmp:
        database = Path(tmp) / "github-inbox.db"
        inbox = GitHubEventInbox(database, authority)
        for index in range(1000):
            body = json.dumps({
                "action": "opened",
                "repository": {"full_name": "ZeoStudio/TenGate"},
                "sender": {"login": "bench-user"},
                "issue": {"number": index},
            }, sort_keys=True, separators=(",", ":")).encode()
            signature = hmac.new(webhook_secret, body, hashlib.sha256).hexdigest()
            headers = {
                "Content-Type": "application/json",
                "X-GitHub-Delivery": f"bench-{index}",
                "X-GitHub-Event": "issues",
                "X-Hub-Signature-256": f"sha256={signature}",
            }
            started = time.perf_counter()
            inbox.register(adapter.verify(headers, body))
            register_ms.append((time.perf_counter() - started) * 1000)
        inbox.close()
        recovered = GitHubEventInbox(database, authority)
        kernel = GateKernel(database, authority)
        bridge = GitHubTaskBridge(adapter, recovered, kernel)
        tasks_created = 0
        for index in range(1000):
            started = time.perf_counter()
            result = bridge.process_one("bench-worker")
            if result is None:
                raise RuntimeError("delivery queue drained early")
            tasks_created += 1
            process_ms.append((time.perf_counter() - started) * 1000)
        stats = recovered.stats()
        kernel.close()
        recovered.close()
    print(json.dumps({
        "schema": "tengate.github-task-bridge-bench.v1",
        "deliveries": 1000,
        "register_ms_p50": round(statistics.median(register_ms), 4),
        "register_ms_p95": round(percentile(register_ms, 0.95), 4),
        "process_ms_p50": round(statistics.median(process_ms), 4),
        "process_ms_p95": round(percentile(process_ms, 0.95), 4),
        "completed": stats["completed"],
        "tasks_created": tasks_created,
        "remaining": stats["received"] + stats["processing"] + stats["failed"],
    }, sort_keys=True))


if __name__ == "__main__":
    main()


#!/usr/bin/env python3
from __future__ import annotations

import json
import statistics
import tempfile
import time
from pathlib import Path

from tengate_v5.kernel import GateKernel
from tengate_v5.identity import HmacSealAuthority
from tengate_v5.models import InboxParcel


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(len(ordered) * fraction))]


def main() -> None:
    authority = HmacSealAuthority(b"relay-bench-key-material-at-least-32-bytes", issuer="bench")
    governor = authority.issue("bench-governor", roles=frozenset({"governor"}))
    recipient = authority.issue("bench-worker", roles=frozenset({"worker"}), lane="bench")
    replay_side_effects = 0
    latencies: list[float] = []
    recovery_latencies: list[float] = []
    recovered_pending: list[int] = []
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "bench.db"
        kernel = GateKernel(db, authority)
        kernel.create_task("TG-BENCH", governor)
        for index in range(1000):
            parcel = InboxParcel("1", f"m-{index}", f"k-{index}", "bench", "TG-BENCH", governor.subject,
                                 (recipient.subject,), "bench", f"2026-10-24T00:00:{index % 60:02d}Z", {"i": index})
            start = time.perf_counter()
            result = kernel.receive(parcel, governor)
            latencies.append((time.perf_counter() - start) * 1000)
            replay_side_effects += int(not result["deliveries"][0]["duplicate"])
            replay = kernel.receive(parcel, governor)
            replay_side_effects += int(not replay["deliveries"][0]["duplicate"])
        kernel.close()
        for _ in range(10):
            start = time.perf_counter()
            recovered = GateKernel(db, authority)
            recovery_latencies.append((time.perf_counter() - start) * 1000)
            recovered_pending.append(len(recovered.pending(recipient.subject)))
            recovered.close()
    print(json.dumps({
        "schema": "tengate.relay-bench.v1", "messages": 1000, "deliver_attempts": 2000,
        "unique_side_effects": replay_side_effects, "restart_runs": len(recovery_latencies),
        "pending_after_restart_min": min(recovered_pending),
        "pending_after_restart_max": max(recovered_pending),
        "delivery_ms_p50": round(statistics.median(latencies), 4),
        "delivery_ms_p95": round(percentile(latencies, 0.95), 4),
        "restart_ms_p50": round(statistics.median(recovery_latencies), 4),
        "restart_ms_p95": round(percentile(recovery_latencies, 0.95), 4),
    }, sort_keys=True))


if __name__ == "__main__":
    main()


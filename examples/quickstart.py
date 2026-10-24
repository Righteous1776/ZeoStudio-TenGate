from __future__ import annotations

import hashlib

from tengate_v5 import GateKernel, HmacSealAuthority


authority = HmacSealAuthority.ephemeral(issuer="quickstart")
governor = authority.issue("governor", roles=frozenset({"governor"}))
worker = authority.issue("worker", roles=frozenset({"worker"}), lane="core")
owner = authority.issue("owner", roles=frozenset({"owner"}))

kernel = GateKernel(":memory:", authority)
kernel.create_task("TG-DEMO", governor)
kernel.transition("TG-DEMO", "assign", governor, expected_revision=0, details={"lane": "core"})
kernel.transition("TG-DEMO", "start", worker, expected_revision=1)
kernel.add_evidence(
    "TG-DEMO",
    "test-output",
    "artifact://demo/tests.txt",
    hashlib.sha256(b"demo passed").hexdigest(),
    worker,
)
kernel.transition("TG-DEMO", "submit_review", worker, expected_revision=2)
result = kernel.transition("TG-DEMO", "accept", owner, expected_revision=3)

print(f"{result.task_id}: {result.state} revision={result.revision}")
print(f"integrity={kernel.verify_integrity(result.task_id)}")
kernel.close()


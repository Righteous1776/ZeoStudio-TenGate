# ZeoStudio TenGate V5.0.0

**Stop prompting agents to cooperate. Give them a system.**

AI Agent 的时代，不该靠聊天记录维持秩序。把混乱变成制度，把遗忘变成可续接的任务，把“我做完了”变成能够校验的证据。

ZeoStudio TenGate V5.0.0 introduces a Python 3.11+ governance and multi-agent coordination **runtime kernel** under Apache-2.0. No password-gated source delivery is required for this version.

## What's included

- **Durable task governance:** revision-controlled state transitions, optimistic concurrency, scoped path leases and task-integrity checks.
- **Persistent collaboration:** idempotent Inbox messaging, receipts, checkpointing and restart recovery.
- **Explicit authority boundaries:** externally verified identity inputs and Owner / Governor / Lane permission gates.
- **GitHub integration:** HMAC-verified raw webhooks, repository bindings, event allowlists, durable event queues, processing leases and policy-checked task bridging.
- **Production-oriented safeguards:** loopback-first HTTP service, bounded request sizes, health and readiness endpoints, controlled background worker shutdown.
- **Recovery tooling:** online SQLite backup, HMAC-protected manifest, read-only database preflight and isolated restore rehearsal.
- **Testable performance:** included unit tests, failure-injection scripts and repeatable benchmarks.

## Getting started

Requires Python 3.11 or later. The runtime has no additional Python package dependencies.

```bash
python -m pip install zeostudio_tengate_v5-5.0.0-py3-none-any.whl
PYTHONPATH=src python examples/quickstart.py
PYTHONPATH=src python -m tengate_v5.cli --config examples/tengate.config.json check-config
```

The first command assumes you downloaded the release wheel into your current directory. For source checkout workflows, see the bundled README.

## Release contents

- Source ZIP (reproducible packaging tool)
- Python wheel
- SPDX 2.3 SBOM
- SHA256SUMS
- RELEASE_MANIFEST.json

The SHA-256 values should be checked after download; hashes alone do not constitute a third-party security certification or a publisher signature.

## Validation status

The V5.0.0 main commit `6808c187f8456fb590f56e7345fbc480e104d7ca` passed GitHub Actions unit checks for Python 3.11, 3.12 and 3.13 and the repository's full reliability gate. Benchmarks are **project-provided**, not independent comparative benchmarks. Packaging and artifact verification are re-run in the controlled manual publish workflow before this release is created.

## Deployment boundary

The service binds locally by default. Production deployments require TLS termination, trusted network source controls, independently verified actor identity, and environment-managed secret rotation. Do not commit webhook secrets, identity seals, backup keys or database snapshots.

**Compatibility:** V5.0.0 is a new runtime kernel; it is not a drop-in replacement for earlier encrypted challenge archives or the separate A/B/C Skill knowledge packages. Review migration requirements before adopting it.

## Attribution

**ZeoStudio · 久野零**

> 协作不是口号，治理才是底座。

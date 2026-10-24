from __future__ import annotations

import hashlib
import hmac
import json
import multiprocessing
import tempfile
import time
from pathlib import Path

from tengate_v5.config import (
    BackupConfig,
    BindingConfig,
    BridgeConfig,
    GitHubConfig,
    IdentityConfig,
    TenGateConfig,
    WorkerConfig,
)
from tengate_v5.github_inbox import GitHubEventInbox
from tengate_v5.identity import HmacSealAuthority
from tengate_v5.kernel import GateKernel
from tengate_v5.runtime import RuntimeStack
from tengate_v5.service import BridgeWorker
from tengate_v5.storage import create_backup, inspect_database


KEY = b"failure-injection-seal-key-material-123456"
WEBHOOK_SECRET = "failure-injection-webhook-secret-123456"
BACKUP_KEY = "failure-injection-backup-key-123456789"


def killed_task_write(database: str, ready: object) -> None:
    authority = HmacSealAuthority(KEY, issuer="failure-injection")
    governor = authority.issue("failure-governor", roles=frozenset({"governor"}))
    kernel = GateKernel(database, authority)
    original = kernel._insert_event

    def pause_after_event(*args: object, **kwargs: object) -> None:
        original(*args, **kwargs)
        ready.set()
        time.sleep(60)

    kernel._insert_event = pause_after_event  # type: ignore[method-assign]
    kernel.transition(
        "TG-FAIL-WRITE",
        "assign",
        governor,
        expected_revision=0,
        event_id="killed-assign",
        details={"lane": "failure-lane"},
    )


def killed_backup(source: str, destination: str, ready: object) -> None:
    def pause_after_page(_: int, __: int, ___: int) -> None:
        ready.set()
        time.sleep(60)

    create_backup(source, destination, progress=pause_after_page)


def killed_worker_lease(database: str, ready: object, results: object) -> None:
    authority = HmacSealAuthority(KEY, issuer="failure-injection")
    inbox = GitHubEventInbox(database, authority, max_attempts=3)
    delivery = inbox.claim("killed-worker", lease_seconds=1)
    results.put(delivery.delivery_id if delivery else "missing")
    ready.set()
    time.sleep(60)


def config_for(database: Path) -> TenGateConfig:
    return TenGateConfig(
        schema_version="1",
        database=database,
        identity=IdentityConfig(issuer="failure-injection", seal_key_env="FAILURE_SEAL_KEY"),
        github=GitHubConfig(
            webhook_secret_env="FAILURE_WEBHOOK_SECRET",
            max_body_bytes=65_536,
            max_attempts=3,
            bindings=(BindingConfig(
                repository="ZeoStudio/TenGate",
                login="failure-owner",
                roles=frozenset({"governor"}),
                lane="failure-lane",
                event_rules=frozenset({"issues:opened"}),
                seal_ttl_seconds=300,
            ),),
        ),
        worker=WorkerConfig(name="recovery-worker", lease_seconds=2, poll_interval_ms=10),
        bridge=BridgeConfig(task_prefix="TG-GH", routes=frozenset({"issues:opened"})),
        backup=BackupConfig(manifest_key_env="FAILURE_BACKUP_KEY"),
    )


def kill_at_ready(context: multiprocessing.context.BaseContext, target: object, args: tuple[object, ...]) -> int:
    ready = context.Event()
    process = context.Process(target=target, args=(*args, ready))
    process.start()
    if not ready.wait(5):
        process.terminate()
        process.join()
        raise RuntimeError("failure-injection child did not reach its kill point")
    process.kill()
    process.join(5)
    if process.is_alive():
        process.terminate()
        process.join()
        raise RuntimeError("failure-injection child did not stop")
    return int(process.exitcode or 0)


def main() -> None:
    context = multiprocessing.get_context("spawn")
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        environment = {
            "FAILURE_SEAL_KEY": KEY.decode(),
            "FAILURE_WEBHOOK_SECRET": WEBHOOK_SECRET,
            "FAILURE_BACKUP_KEY": BACKUP_KEY,
        }

        write_database = root / "write.db"
        authority = HmacSealAuthority(KEY, issuer="failure-injection")
        governor = authority.issue("failure-governor", roles=frozenset({"governor"}))
        kernel = GateKernel(write_database, authority)
        kernel.create_task("TG-FAIL-WRITE", governor, event_id="create-write")
        kernel.close()
        write_exit = kill_at_ready(context, killed_task_write, (str(write_database),))
        kernel = GateKernel(write_database, authority)
        write_rolled_back = (
            kernel.get_task("TG-FAIL-WRITE").revision == 0
            and kernel.verify_integrity("TG-FAIL-WRITE")
        )
        kernel.close()

        backup_source = root / "backup-source.db"
        config = config_for(backup_source)
        with RuntimeStack.open(config, environment) as runtime:
            for number in range(50):
                runtime.kernel.create_task(f"TG-BACKUP-{number}", governor, event_id=f"backup-{number}")
        backup_destination = root / "published-backup.db"
        backup_exit = kill_at_ready(
            context, killed_backup, (str(backup_source), str(backup_destination))
        )
        source_intact = inspect_database(backup_source).compatible
        partial_not_published = not backup_destination.exists()

        worker_database = root / "worker.db"
        worker_config = config_for(worker_database)
        with RuntimeStack.open(worker_config, environment) as runtime:
            body = json.dumps({
                "action": "opened",
                "repository": {"full_name": "ZeoStudio/TenGate"},
                "sender": {"login": "failure-owner"},
                "issue": {"number": 1},
            }, sort_keys=True, separators=(",", ":")).encode()
            signature = hmac.new(WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()
            event = runtime.adapter.verify({
                "Content-Type": "application/json",
                "X-GitHub-Delivery": "failure-worker-delivery",
                "X-GitHub-Event": "issues",
                "X-Hub-Signature-256": f"sha256={signature}",
            }, body)
            runtime.inbox.register(event)

        ready = context.Event()
        lease_results = context.Queue()
        process = context.Process(
            target=killed_worker_lease,
            args=(str(worker_database), ready, lease_results),
        )
        process.start()
        if not ready.wait(5) or lease_results.get(timeout=2) != "failure-worker-delivery":
            process.terminate()
            process.join()
            raise RuntimeError("worker did not claim the delivery before kill")
        process.kill()
        process.join(5)
        worker_exit = int(process.exitcode or 0)
        time.sleep(1.1)
        with RuntimeStack.open(worker_config, environment) as runtime:
            worker = BridgeWorker(
                runtime.bridge,
                worker_config.worker.name,
                lease_seconds=worker_config.worker.lease_seconds,
                poll_interval_ms=worker_config.worker.poll_interval_ms,
            )
            worker.start()
            deadline = time.monotonic() + 5
            while runtime.inbox.stats()["completed"] != 1 and time.monotonic() < deadline:
                time.sleep(0.01)
            worker.stop()
            recovered_delivery = runtime.inbox.get("failure-worker-delivery")
            worker_recovered = (
                recovered_delivery.state.value == "completed"
                and recovered_delivery.attempts == 2
                and worker.snapshot().processed == 1
            )

        if not all((write_rolled_back, source_intact, partial_not_published, worker_recovered)):
            raise RuntimeError("failure-injection invariant failed")
        print(json.dumps({
            "backup_child_exit": backup_exit,
            "backup_partial_published": not partial_not_published,
            "backup_source_intact": source_intact,
            "schema": "tengate.failure-injection.v1",
            "task_write_child_exit": write_exit,
            "task_write_rolled_back": write_rolled_back,
            "worker_child_exit": worker_exit,
            "worker_recovered": worker_recovered,
            "worker_attempts": 2,
        }, sort_keys=True))


if __name__ == "__main__":
    main()

from __future__ import annotations

import hashlib
import multiprocessing
import sqlite3
import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path

from tengate_v5.capabilities import CapabilityPrism, require_capability
from tengate_v5.errors import (
    AuthorizationDenied, IntegrityFailure, InvalidTransition, RevisionConflict, UnsupportedCapability,
)
from tengate_v5.kernel import GateKernel
from tengate_v5.identity import HmacSealAuthority
from tengate_v5.models import ActorSeal, InboxParcel, TaskState


AUTHORITY = HmacSealAuthority(b"test-key-material-must-be-at-least-32-bytes", issuer="test-oidc")
OWNER = AUTHORITY.issue("owner-1", roles=frozenset({"owner"}))
GOV = AUTHORITY.issue("gov-1", roles=frozenset({"governor"}))
WORKER = AUTHORITY.issue("worker-1", roles=frozenset({"worker"}), lane="lane-a")
OTHER = AUTHORITY.issue("worker-2", roles=frozenset({"worker"}), lane="lane-b")


def _process_assign(database: str, event_id: str, start: object, results: object) -> None:
    authority = HmacSealAuthority(b"test-key-material-must-be-at-least-32-bytes", issuer="test-oidc")
    governor = authority.issue("gov-1", roles=frozenset({"governor"}))
    kernel = GateKernel(database, authority)
    start.wait()
    try:
        kernel.transition(
            "TG-P", "assign", governor, expected_revision=0,
            event_id=event_id, details={"lane": "lane-a"},
        )
        results.put("committed")
    except RevisionConflict:
        results.put("conflict")
    except Exception as exc:  # pragma: no cover - reported to parent for diagnosis
        results.put(f"error:{type(exc).__name__}:{exc}")
    finally:
        kernel.close()


class KernelTests(unittest.TestCase):
    def setUp(self) -> None:
        self.kernel = GateKernel(":memory:", AUTHORITY)
        self.kernel.create_task("TG-X", GOV, event_id="create-1")

    def tearDown(self) -> None:
        self.kernel.close()

    def test_governed_happy_path_and_hash_chain(self) -> None:
        task = self.kernel.transition("TG-X", "assign", GOV, expected_revision=0, details={"lane": "lane-a"})
        self.assertEqual((task.state, task.revision), (TaskState.ASSIGNED, 1))
        task = self.kernel.transition("TG-X", "start", WORKER, expected_revision=1)
        evidence_hash = hashlib.sha256(b"real output").hexdigest()
        evidence_id = self.kernel.add_evidence("TG-X", "test-log", "artifact://local/test.log", evidence_hash, WORKER)
        self.kernel.checkpoint("TG-X", WORKER, summary="implementation complete", next_action="review", last_evidence=evidence_id)
        task = self.kernel.transition("TG-X", "submit_review", WORKER, expected_revision=2)
        task = self.kernel.transition("TG-X", "accept", OWNER, expected_revision=3)
        self.assertEqual(task.state, TaskState.ACCEPTED)
        self.assertTrue(self.kernel.verify_chain("TG-X"))

    def test_worker_cannot_start_queued_or_accept_review(self) -> None:
        with self.assertRaises(InvalidTransition):
            self.kernel.transition("TG-X", "start", WORKER, expected_revision=0)
        self.kernel.transition("TG-X", "assign", GOV, expected_revision=0, details={"lane": "lane-a"})
        self.kernel.transition("TG-X", "start", WORKER, expected_revision=1)
        self.kernel.add_evidence("TG-X", "commit", "git:abc", "a" * 64, WORKER)
        self.kernel.transition("TG-X", "submit_review", WORKER, expected_revision=2)
        with self.assertRaises(AuthorizationDenied):
            self.kernel.transition("TG-X", "accept", WORKER, expected_revision=3)

    def test_lane_evidence_revision_and_event_id_guards(self) -> None:
        self.kernel.transition("TG-X", "assign", GOV, expected_revision=0, details={"lane": "lane-a"})
        with self.assertRaises(AuthorizationDenied):
            self.kernel.transition("TG-X", "start", OTHER, expected_revision=1)
        task = self.kernel.transition("TG-X", "start", WORKER, expected_revision=1, event_id="start-1")
        duplicate = self.kernel.transition("TG-X", "start", WORKER, expected_revision=1, event_id="start-1")
        self.assertEqual(duplicate.revision, task.revision)
        with self.assertRaises(RevisionConflict):
            self.kernel.transition("TG-X", "block", WORKER, expected_revision=1)
        with self.assertRaises(IntegrityFailure):
            self.kernel.transition("TG-X", "submit_review", WORKER, expected_revision=2)

    def test_unverified_claim_never_grants_owner(self) -> None:
        forged = replace(WORKER, roles=frozenset({"owner"}))
        with self.assertRaises(AuthorizationDenied):
            self.kernel.transition("TG-X", "assign", forged, expected_revision=0, details={"lane": "lane-a"})
        malformed_time = replace(OWNER, issued_at="2026-01-01", expires_at="2026-01-02")
        with self.assertRaises(AuthorizationDenied):
            self.kernel.transition("TG-X", "assign", malformed_time, expected_revision=0, details={"lane": "lane-a"})

    def test_parcel_at_least_once_dedup_ack_and_secret_guard(self) -> None:
        parcel = InboxParcel("1", "m-1", "idem-1", "p", "TG-X", "claimed-owner", ("worker-1",),
                             "task", "2026-10-24T00:00:00Z", {"action": "inspect"})
        first = self.kernel.receive(parcel, GOV)
        second = self.kernel.receive(parcel, GOV)
        self.assertFalse(first["deliveries"][0]["duplicate"])
        self.assertTrue(second["deliveries"][0]["duplicate"])
        same_effect = InboxParcel("1", "m-2", "idem-1", "p", "TG-X", "claimed-owner", ("worker-1",),
                                  "task", "2026-10-24T00:00:00Z", {"action": "inspect"})
        # A new message id may retry the same logical effect only if content remains identical.
        self.assertTrue(self.kernel.receive(same_effect, GOV)["deliveries"][0]["duplicate"])
        altered = InboxParcel("1", "m-1", "idem-1", "p", "TG-X", "claimed-owner", ("worker-1",),
                              "task", "2026-10-24T00:00:00Z", {"action": "altered"})
        with self.assertRaises(IntegrityFailure):
            self.kernel.receive(altered, GOV)
        self.assertEqual(len(self.kernel.pending("worker-1")), 1)
        self.assertTrue(self.kernel.acknowledge("m-1", "worker-1", WORKER))
        self.assertFalse(self.kernel.acknowledge("m-1", "worker-1", WORKER))
        self.assertEqual(self.kernel.get_task("TG-X").state, TaskState.QUEUED)  # Ack is not completion.
        leaked = InboxParcel("1", "m-3", "idem-3", "p", "TG-X", "x", ("worker-1",), "task",
                             "2026-10-24T00:00:02Z", {"token": "ghp_abcdefghijklmnopqrstuvwxyz1234567890"})
        with self.assertRaises(IntegrityFailure):
            self.kernel.receive(leaked, GOV)

    def test_one_message_can_reach_multiple_recipients(self) -> None:
        parcel = InboxParcel("1", "m-many", "idem-many", "p", "TG-X", "gov-1",
                             ("worker-1", "worker-2"), "notice", "2026-10-24T00:00:00Z", {})
        result = self.kernel.receive(parcel, GOV)
        self.assertEqual([item["duplicate"] for item in result["deliveries"]], [False, False])
        self.assertEqual(len(self.kernel.pending("worker-1")), 1)
        self.assertEqual(len(self.kernel.pending("worker-2")), 1)

    def test_parcel_schema_and_task_reference_are_enforced(self) -> None:
        unsupported = InboxParcel("2", "m-v2", "k-v2", "p", "TG-X", "gov-1", ("worker-1",),
                                  "notice", "2026-10-24T00:00:00Z", {})
        with self.assertRaises(ValueError):
            self.kernel.receive(unsupported, GOV)
        duplicate_recipient = InboxParcel("1", "m-dup", "k-dup", "p", "TG-X", "gov-1",
                                          ("worker-1", "worker-1"), "notice",
                                          "2026-10-24T00:00:00Z", {})
        with self.assertRaises(ValueError):
            self.kernel.receive(duplicate_recipient, GOV)
        missing_task = InboxParcel("1", "m-orphan", "k-orphan", "p", "TG-MISSING", "gov-1",
                                   ("worker-1",), "notice", "2026-10-24T00:00:00Z", {})
        with self.assertRaises(KeyError):
            self.kernel.receive(missing_task, GOV)

    def test_file_backed_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "gate.db"
            one = GateKernel(db, AUTHORITY)
            one.create_task("TG-R", GOV)
            one.transition("TG-R", "assign", GOV, expected_revision=0, details={"lane": "lane-a"})
            one.close()
            two = GateKernel(db, AUTHORITY)
            self.assertEqual(two.get_task("TG-R").revision, 1)
            self.assertTrue(two.verify_chain("TG-R"))
            two.close()

    def test_lease_detects_overlap_and_never_defeats_revision(self) -> None:
        self.kernel.transition("TG-X", "assign", GOV, expected_revision=0, details={"lane": "lane-a"})
        self.kernel.transition("TG-X", "start", WORKER, expected_revision=1)
        lease = self.kernel.acquire_lease("TG-X", "src/core", WORKER, expected_revision=2)
        self.assertTrue(self.kernel.validate_lease(lease, WORKER))
        with self.assertRaises(RevisionConflict):
            self.kernel.acquire_lease("TG-X", "src/core/file.py", WORKER, expected_revision=2)
        self.kernel.transition("TG-X", "block", WORKER, expected_revision=2)
        self.assertFalse(self.kernel.validate_lease(lease, WORKER))
        self.assertTrue(self.kernel.release_lease(lease, WORKER))

    def test_capability_downgrade_is_explicit(self) -> None:
        class Adapter:
            def capabilities(self) -> CapabilityPrism:
                return CapabilityPrism(fetch_pending=True, dispatch=False)

            def health(self) -> dict[str, str]:
                return {"status": "ok"}

        require_capability(Adapter(), "fetch_pending")
        with self.assertRaises(UnsupportedCapability):
            require_capability(Adapter(), "dispatch")

    def test_chain_verification_detects_tamper(self) -> None:
        self.kernel.transition("TG-X", "assign", GOV, expected_revision=0, details={"lane": "lane-a"})
        self.assertTrue(self.kernel.verify_chain("TG-X"))
        self.kernel._db.execute("UPDATE task_events SET details_json='{}' WHERE action='assign'")
        self.assertFalse(self.kernel.verify_chain("TG-X"))

    def test_chain_verification_detects_event_deletion(self) -> None:
        self.kernel.transition("TG-X", "assign", GOV, expected_revision=0, details={"lane": "lane-a"})
        self.kernel.transition("TG-X", "start", WORKER, expected_revision=1)
        self.kernel._db.execute("DELETE FROM task_events WHERE revision=1")
        self.assertFalse(self.kernel.verify_chain("TG-X"))

    def test_evidence_and_checkpoint_integrity_detects_tamper(self) -> None:
        self.kernel.transition("TG-X", "assign", GOV, expected_revision=0, details={"lane": "lane-a"})
        self.kernel.transition("TG-X", "start", WORKER, expected_revision=1)
        evidence_id = self.kernel.add_evidence("TG-X", "test", "artifact:test", "b" * 64, WORKER)
        self.kernel.checkpoint("TG-X", WORKER, summary="done", next_action="review", last_evidence=evidence_id)
        self.assertTrue(self.kernel.verify_integrity("TG-X"))
        self.kernel._db.execute("UPDATE evidence SET immutable_ref='artifact:changed'")
        self.assertFalse(self.kernel.verify_integrity("TG-X"))

    def test_two_connections_cannot_commit_same_revision(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "concurrent.db"
            first = GateKernel(db, AUTHORITY)
            second = GateKernel(db, AUTHORITY)
            first.create_task("TG-C", GOV)
            barrier = threading.Barrier(2)
            outcomes: list[str] = []

            def assign(kernel: GateKernel, event_id: str) -> None:
                barrier.wait()
                try:
                    kernel.transition(
                        "TG-C", "assign", GOV, expected_revision=0,
                        event_id=event_id, details={"lane": "lane-a"},
                    )
                    outcomes.append("committed")
                except RevisionConflict:
                    outcomes.append("conflict")

            threads = [
                threading.Thread(target=assign, args=(first, "assign-a")),
                threading.Thread(target=assign, args=(second, "assign-b")),
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            self.assertCountEqual(outcomes, ["committed", "conflict"])
            self.assertEqual(first.get_task("TG-C").revision, 1)
            first.close()
            second.close()

    def test_two_processes_cannot_commit_same_revision(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "processes.db"
            setup = GateKernel(db, AUTHORITY)
            setup.create_task("TG-P", GOV)
            setup.close()
            context = multiprocessing.get_context("spawn")
            start = context.Event()
            results = context.Queue()
            processes = [
                context.Process(target=_process_assign, args=(str(db), "process-a", start, results)),
                context.Process(target=_process_assign, args=(str(db), "process-b", start, results)),
            ]
            for process in processes:
                process.start()
            start.set()
            outcomes = [results.get(timeout=10) for _ in processes]
            for process in processes:
                process.join(timeout=10)
                self.assertFalse(process.is_alive())
                self.assertEqual(process.exitcode, 0)
            self.assertCountEqual(outcomes, ["committed", "conflict"])
            verify = GateKernel(db, AUTHORITY)
            self.assertEqual(verify.get_task("TG-P").revision, 1)
            self.assertTrue(verify.verify_chain("TG-P"))
            verify.close()

    def test_newer_database_schema_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "future.db"
            connection = sqlite3.connect(db)
            connection.execute("PRAGMA user_version=99")
            connection.close()
            with self.assertRaises(IntegrityFailure):
                GateKernel(db, AUTHORITY)


if __name__ == "__main__":
    unittest.main()


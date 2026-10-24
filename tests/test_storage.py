from __future__ import annotations

import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from tengate_v5.cli import main
from tengate_v5.config import load_config
from tengate_v5.errors import IntegrityFailure
from tengate_v5.runtime import RuntimeStack
from tengate_v5.storage import (
    create_backup, create_signed_backup, inspect_database, rehearse_restore, verify_backup_manifest,
)


SEAL_SECRET = "storage-seal-secret-value-12345678901"
WEBHOOK_SECRET = "storage-webhook-secret-value-12345678"
BACKUP_SECRET = "storage-backup-secret-value-1234567890"


class StorageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.config_path = self.root / "config.json"
        self.database = self.root / "tengate.db"
        self.config_path.write_text(json.dumps({
            "schema_version": "1",
            "database": "tengate.db",
            "identity": {"issuer": "storage-tests", "seal_key_env": "STORAGE_SEAL_KEY"},
            "github": {
                "webhook_secret_env": "STORAGE_WEBHOOK_SECRET",
                "max_body_bytes": 4096,
                "max_attempts": 3,
                "bindings": [{
                    "repository": "ZeoStudio/TenGate",
                    "login": "owner",
                    "roles": ["governor"],
                    "lane": "github-intake",
                    "event_rules": ["issues:opened"],
                    "seal_ttl_seconds": 300,
                }],
            },
            "worker": {"name": "storage-worker", "lease_seconds": 60},
            "bridge": {"task_prefix": "TG-GH", "routes": ["issues:opened"]},
            "backup": {"manifest_key_env": "STORAGE_BACKUP_KEY"},
        }), encoding="utf-8")
        self.environ = {
            "STORAGE_SEAL_KEY": SEAL_SECRET,
            "STORAGE_WEBHOOK_SECRET": WEBHOOK_SECRET,
            "STORAGE_BACKUP_KEY": BACKUP_SECRET,
        }
        self.config = load_config(self.config_path)
        self.runtime = RuntimeStack.open(self.config, self.environ)
        actor = self.runtime.authority.issue("storage-governor", roles=frozenset({"governor"}))
        self.runtime.kernel.create_task("TG-STORAGE", actor, event_id="storage-create")

    def tearDown(self) -> None:
        self.runtime.close()
        self.tmp.cleanup()

    def test_live_backup_is_compatible_and_preserves_task(self) -> None:
        destination = self.root / "backup.db"
        report = create_backup(self.database, destination)
        self.assertTrue(report.compatible)
        self.assertEqual(len(report.file_sha256), 64)
        self.assertEqual((report.task_count, report.verified_tasks), (1, 1))
        with RuntimeStack.open(
            self.config.__class__(
                schema_version=self.config.schema_version,
                database=destination,
                identity=self.config.identity,
                github=self.config.github,
                worker=self.config.worker,
                bridge=self.config.bridge,
            ),
            self.environ,
        ) as restored:
            self.assertEqual(restored.kernel.get_task("TG-STORAGE").revision, 0)
            self.assertTrue(restored.kernel.verify_integrity("TG-STORAGE"))

    def test_backup_never_overwrites_destination(self) -> None:
        destination = self.root / "backup.db"
        destination.write_bytes(b"keep-me")
        with self.assertRaises(FileExistsError):
            create_backup(self.database, destination)
        self.assertEqual(destination.read_bytes(), b"keep-me")

    def test_corrupt_database_fails_closed(self) -> None:
        corrupt = self.root / "corrupt.db"
        corrupt.write_bytes(b"not sqlite")
        with self.assertRaises(IntegrityFailure):
            inspect_database(corrupt)

    def test_newer_schema_is_reported_incompatible_without_mutation(self) -> None:
        newer = self.root / "newer.db"
        connection = sqlite3.connect(newer)
        connection.execute("PRAGMA user_version=2")
        connection.commit()
        connection.close()
        before = newer.read_bytes()
        report = inspect_database(newer)
        self.assertFalse(report.compatible)
        self.assertTrue(any("newer" in issue for issue in report.issues))
        self.assertEqual(newer.read_bytes(), before)

    def test_signed_backup_and_isolated_restore_rehearsal(self) -> None:
        destination = self.root / "signed.db"
        manifest_path = self.root / "signed.manifest.json"
        key = BACKUP_SECRET.encode()
        result = create_signed_backup(self.database, destination, manifest_path, key)
        self.assertEqual(result.manifest.backup_filename, destination.name)
        self.assertEqual(verify_backup_manifest(destination, manifest_path, key), result.manifest)
        files_before = {path.name for path in self.root.iterdir()}
        rehearsal = rehearse_restore(destination, manifest_path, key)
        self.assertEqual(rehearsal.status, "verified")
        self.assertEqual(rehearsal.logical_sha256, result.manifest.logical_sha256)
        self.assertEqual({path.name for path in self.root.iterdir()}, files_before)

    def test_manifest_or_backup_tampering_fails_closed(self) -> None:
        destination = self.root / "signed.db"
        manifest_path = self.root / "signed.manifest.json"
        key = BACKUP_SECRET.encode()
        create_signed_backup(self.database, destination, manifest_path, key)
        document = json.loads(manifest_path.read_text(encoding="utf-8"))
        document["user_version"] = 99
        manifest_path.write_text(json.dumps(document), encoding="utf-8")
        with self.assertRaises(IntegrityFailure):
            verify_backup_manifest(destination, manifest_path, key)

        manifest_path.unlink()
        create_signed_backup(self.database, self.root / "second.db", manifest_path, key)
        second = self.root / "second.db"
        second.write_bytes(second.read_bytes() + b"tampered")
        with self.assertRaises(IntegrityFailure):
            rehearse_restore(second, manifest_path, key)

    def test_application_chain_tampering_prevents_signed_backup(self) -> None:
        self.runtime.kernel._db.execute(
            "UPDATE task_events SET details_json='{}' WHERE task_id='TG-STORAGE'"
        )
        destination = self.root / "rejected.db"
        manifest_path = self.root / "rejected.manifest.json"
        with self.assertRaises(IntegrityFailure):
            create_signed_backup(
                self.database, destination, manifest_path, BACKUP_SECRET.encode()
            )
        self.assertFalse(destination.exists())
        self.assertFalse(manifest_path.exists())

    def test_cli_check_backup_and_rehearsal_need_only_backup_key(self) -> None:
        output = io.StringIO()
        errors = io.StringIO()
        code = main(
            ["--config", str(self.config_path), "check-database"],
            environ={}, stdout=output, stderr=errors,
        )
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.getvalue())["status"], "compatible")
        output = io.StringIO()
        destination = self.root / "cli-backup.db"
        manifest = self.root / "cli-backup.manifest.json"
        code = main(
            [
                "--config", str(self.config_path), "backup", "--output", str(destination),
                "--manifest", str(manifest),
            ],
            environ={"STORAGE_BACKUP_KEY": BACKUP_SECRET}, stdout=output, stderr=errors,
        )
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.getvalue())["status"], "created")
        self.assertTrue(destination.is_file())
        self.assertTrue(manifest.is_file())
        self.assertNotIn(BACKUP_SECRET, output.getvalue())
        output = io.StringIO()
        code = main(
            [
                "--config", str(self.config_path), "rehearse-restore",
                "--backup", str(destination), "--manifest", str(manifest),
            ],
            environ={"STORAGE_BACKUP_KEY": BACKUP_SECRET}, stdout=output, stderr=errors,
        )
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.getvalue())["status"], "verified")
        self.assertNotIn(BACKUP_SECRET, output.getvalue())

    def test_wrong_manifest_key_is_rejected_without_disclosure(self) -> None:
        destination = self.root / "signed.db"
        manifest_path = self.root / "signed.manifest.json"
        create_signed_backup(self.database, destination, manifest_path, BACKUP_SECRET.encode())
        with self.assertRaises(IntegrityFailure) as raised:
            verify_backup_manifest(destination, manifest_path, b"wrong-key-material-value-1234567890")
        self.assertNotIn(BACKUP_SECRET, str(raised.exception))


if __name__ == "__main__":
    unittest.main()

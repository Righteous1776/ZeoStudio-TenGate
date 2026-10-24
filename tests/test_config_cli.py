from __future__ import annotations

import hashlib
import hmac
import io
import json
import tempfile
import unittest
from pathlib import Path

from tengate_v5.cli import main
from tengate_v5.config import load_config, resolve_secrets


SEAL_SECRET = "config-cli-seal-secret-value-123456789"
WEBHOOK_SECRET = "config-cli-webhook-secret-value-123456"


def config_document(database: str) -> dict[str, object]:
    return {
        "schema_version": "1",
        "database": database,
        "identity": {"issuer": "cli-tests", "seal_key_env": "TEST_SEAL_KEY"},
        "github": {
            "webhook_secret_env": "TEST_WEBHOOK_SECRET",
            "max_body_bytes": 65536,
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
        "worker": {"name": "cli-worker", "lease_seconds": 60},
        "bridge": {"task_prefix": "TG-GH", "routes": ["issues:opened"]},
    }


class ConfigAndCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.config_path = self.root / "tengate.json"
        self.database = self.root / "tengate.db"
        self.config_path.write_text(json.dumps(config_document("tengate.db")), encoding="utf-8")
        self.environ = {"TEST_SEAL_KEY": SEAL_SECRET, "TEST_WEBHOOK_SECRET": WEBHOOK_SECRET}

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def run_cli(self, *args: str, environ: dict[str, str] | None = None) -> tuple[int, dict, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        code = main(
            ["--config", str(self.config_path), *args],
            environ=self.environ if environ is None else environ,
            stdout=stdout,
            stderr=stderr,
        )
        payload = json.loads(stdout.getvalue()) if stdout.getvalue() else json.loads(stderr.getvalue())
        return code, payload, stderr.getvalue()

    def test_static_configuration_validation_does_not_need_secrets(self) -> None:
        code, payload, _ = self.run_cli("check-config", environ={})
        self.assertEqual(code, 0)
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["database"], str(self.database))
        self.assertEqual(payload["bindings"], 1)

    def test_secrets_are_environment_only_and_redacted_from_repr(self) -> None:
        config = load_config(self.config_path)
        secrets = resolve_secrets(config, self.environ)
        self.assertNotIn(SEAL_SECRET, repr(secrets))
        self.assertNotIn(WEBHOOK_SECRET, repr(secrets))
        code, payload, stderr = self.run_cli("init", environ={})
        self.assertEqual(code, 2)
        self.assertEqual(payload["error"], "ValueError")
        self.assertNotIn(SEAL_SECRET, stderr)
        self.assertNotIn(WEBHOOK_SECRET, stderr)

    def test_duplicate_and_unknown_configuration_keys_are_rejected(self) -> None:
        self.config_path.write_text(
            '{"schema_version":"1","schema_version":"1"}', encoding="utf-8"
        )
        with self.assertRaises(ValueError):
            load_config(self.config_path)
        document = config_document("tengate.db")
        document["unexpected"] = True
        self.config_path.write_text(json.dumps(document), encoding="utf-8")
        with self.assertRaises(ValueError):
            load_config(self.config_path)

    def test_cli_receive_process_status_and_verify(self) -> None:
        code, initialized, _ = self.run_cli("init")
        self.assertEqual((code, initialized["status"]), (0, "initialized"))
        body = json.dumps({
            "action": "opened",
            "repository": {"full_name": "ZeoStudio/TenGate"},
            "sender": {"login": "octo-owner"},
            "issue": {"number": 99},
        }, sort_keys=True, separators=(",", ":")).encode()
        body_path = self.root / "body.json"
        body_path.write_bytes(body)
        signature = hmac.new(WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()
        header_path = self.root / "headers.json"
        header_path.write_text(json.dumps({
            "Content-Type": "application/json",
            "X-GitHub-Delivery": "cli-delivery-1",
            "X-GitHub-Event": "issues",
            "X-Hub-Signature-256": f"sha256={signature}",
        }), encoding="utf-8")
        code, received, _ = self.run_cli("receive", "--headers", str(header_path), "--body", str(body_path))
        self.assertEqual(code, 0)
        self.assertTrue(received["registered"])
        code, processed, _ = self.run_cli("process-one")
        self.assertEqual((code, processed["status"]), (0, "completed"))
        task_id = processed["task_id"]
        code, status, _ = self.run_cli("status")
        self.assertEqual(code, 0)
        self.assertEqual(status["inbox"]["completed"], 1)
        code, verified, _ = self.run_cli("verify-task", task_id)
        self.assertEqual((code, verified["status"]), (0, "valid"))

    def test_cli_rejects_tampered_webhook_without_secret_disclosure(self) -> None:
        body_path = self.root / "body.json"
        body_path.write_text("{}", encoding="utf-8")
        header_path = self.root / "headers.json"
        header_path.write_text(json.dumps({
            "Content-Type": "application/json",
            "X-GitHub-Delivery": "cli-bad-1",
            "X-GitHub-Event": "issues",
            "X-Hub-Signature-256": "sha256=" + "0" * 64,
        }), encoding="utf-8")
        code, payload, stderr = self.run_cli(
            "receive", "--headers", str(header_path), "--body", str(body_path)
        )
        self.assertEqual(code, 2)
        self.assertEqual(payload["error"], "WebhookRejected")
        self.assertNotIn(SEAL_SECRET, stderr)
        self.assertNotIn(WEBHOOK_SECRET, stderr)


if __name__ == "__main__":
    unittest.main()


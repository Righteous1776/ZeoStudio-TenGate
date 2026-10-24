from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import threading
from pathlib import Path
from typing import Mapping, Sequence, TextIO

from .config import load_config, resolve_backup_key
from .errors import TenGateError
from .runtime import RuntimeStack
from .server import create_server
from .service import BridgeWorker
from .storage import create_signed_backup, inspect_database, rehearse_restore


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tengate-v5")
    parser.add_argument("--config", required=True, help="path to TenGate JSON configuration")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("check-config", help="validate configuration without resolving secrets")
    commands.add_parser("init", help="initialize the configured database")
    commands.add_parser("status", help="show durable inbox counts")
    commands.add_parser("check-database", help="run read-only schema and integrity preflight")
    backup = commands.add_parser("backup", help="create and verify a non-overwriting SQLite backup")
    backup.add_argument("--output", required=True)
    backup.add_argument("--manifest")
    rehearse = commands.add_parser("rehearse-restore", help="verify and restore a backup only in a temp directory")
    rehearse.add_argument("--backup", required=True)
    rehearse.add_argument("--manifest", required=True)

    serve = commands.add_parser("serve", help="serve health and GitHub webhook endpoints")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8080)
    serve.add_argument("--no-worker", action="store_true", help="receive only; do not process events")

    receive = commands.add_parser("receive", help="verify and persist one GitHub webhook")
    receive.add_argument("--headers", required=True, help="JSON file containing webhook headers")
    receive.add_argument("--body", required=True, help="file containing the raw webhook body")

    process = commands.add_parser("process-one", help="process at most one durable event")
    process.add_argument("--worker", help="override configured worker name")

    verify = commands.add_parser("verify-task", help="verify one task integrity chain")
    verify.add_argument("task_id")
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    output = stdout or sys.stdout
    errors = stderr or sys.stderr
    args = build_parser().parse_args(argv)
    try:
        config = load_config(args.config)
        if args.command == "check-config":
            return _emit(output, {
                "status": "ok",
                "schema_version": config.schema_version,
                "database": str(config.database),
                "bindings": len(config.github.bindings),
                "routes": sorted(config.bridge.routes),
            })
        if args.command == "check-database":
            report = inspect_database(config.database)
            _emit(output, {"status": "compatible" if report.compatible else "incompatible", **report.as_dict()})
            return 0 if report.compatible else 4
        runtime_environ = os.environ if environ is None else environ
        if args.command == "backup":
            manifest_path = args.manifest or f"{args.output}.manifest.json"
            result = create_signed_backup(
                config.database, args.output, manifest_path, resolve_backup_key(config, runtime_environ)
            )
            return _emit(output, {"status": "created", **result.as_dict()})
        if args.command == "rehearse-restore":
            result = rehearse_restore(
                args.backup, args.manifest, resolve_backup_key(config, runtime_environ)
            )
            return _emit(output, result.as_dict())
        with RuntimeStack.open(config, runtime_environ) as runtime:
            if args.command == "init":
                return _emit(output, {"status": "initialized", "database": str(config.database)})
            if args.command == "status":
                return _emit(output, {"status": "ok", "inbox": runtime.inbox.stats()})
            if args.command == "serve":
                worker = None if args.no_worker else BridgeWorker(
                    runtime.bridge,
                    config.worker.name,
                    lease_seconds=config.worker.lease_seconds,
                    poll_interval_ms=config.worker.poll_interval_ms,
                )
                server = create_server(runtime, args.host, args.port, worker=worker)
                previous_sigterm = None
                worker_stopped = True
                try:
                    if worker is not None:
                        worker.start()
                    host, port = server.server_address[:2]
                    _emit(output, {
                        "status": "listening",
                        "host": host,
                        "port": port,
                        "worker": worker is not None,
                    })
                    if threading.current_thread() is threading.main_thread():
                        previous_sigterm = signal.signal(signal.SIGTERM, _interrupt_service)
                    try:
                        server.serve_forever()
                    except KeyboardInterrupt:
                        pass
                finally:
                    server.server_close()
                    if worker is not None:
                        worker_stopped = worker.stop()
                    if previous_sigterm is not None:
                        signal.signal(signal.SIGTERM, previous_sigterm)
                if not worker_stopped:
                    raise RuntimeError("worker did not stop before the shutdown deadline")
                return 0
            if args.command == "receive":
                header_path = Path(args.headers)
                body_path = Path(args.body)
                if not header_path.is_file() or header_path.stat().st_size > 65_536:
                    raise ValueError("headers path must be a JSON file no larger than 64 KiB")
                if not body_path.is_file() or body_path.stat().st_size > config.github.max_body_bytes:
                    raise ValueError("body path exceeds the configured webhook size limit")
                header_data = json.loads(header_path.read_text(encoding="utf-8"))
                if not isinstance(header_data, dict) or any(
                    not isinstance(name, str) or not isinstance(value, str)
                    for name, value in header_data.items()
                ):
                    raise ValueError("headers file must contain a string-to-string JSON object")
                event = runtime.adapter.verify(header_data, body_path.read_bytes())
                registered = runtime.inbox.register(event)
                return _emit(output, {
                    "status": "received",
                    "delivery_id": event.delivery_id,
                    "registered": registered,
                    "resource_ref": event.resource_ref,
                })
            if args.command == "process-one":
                worker = args.worker or config.worker.name
                result = runtime.bridge.process_one(worker, lease_seconds=config.worker.lease_seconds)
                if result is None:
                    return _emit(output, {"status": "idle"})
                return _emit(output, {
                    "status": "completed",
                    "delivery_id": result.delivery_id,
                    "task_id": result.task_id,
                    "task_revision": result.task_revision,
                })
            if args.command == "verify-task":
                task = runtime.kernel.get_task(args.task_id)
                valid = runtime.kernel.verify_integrity(args.task_id)
                _emit(output, {
                    "status": "valid" if valid else "invalid",
                    "task_id": task.task_id,
                    "task_state": task.state,
                    "revision": task.revision,
                })
                return 0 if valid else 4
        raise RuntimeError("unhandled command")
    except (OSError, RuntimeError, ValueError, KeyError, TenGateError, json.JSONDecodeError) as exc:
        _emit(errors, {"status": "error", "error": type(exc).__name__, "message": str(exc)})
        return 2


def _emit(stream: TextIO, payload: Mapping[str, object]) -> int:
    stream.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
    stream.flush()
    return 0


def _interrupt_service(*_: object) -> None:
    raise KeyboardInterrupt


if __name__ == "__main__":
    raise SystemExit(main())


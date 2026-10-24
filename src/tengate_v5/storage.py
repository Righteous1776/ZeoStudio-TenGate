from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import sqlite3
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable

from .errors import IntegrityFailure
from .integrity import digest


_REQUIRED_TABLES = frozenset({
    "tasks",
    "task_events",
    "evidence",
    "checkpoints",
    "parcels",
    "leases",
    "tengate_component_versions",
    "github_event_inbox",
})
_SUPPORTED_USER_VERSION = 1
_SUPPORTED_COMPONENTS = {"github_event_inbox": 1}
_MANIFEST_SCHEMA = "tengate.backup-manifest.v1"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True, slots=True)
class DatabaseReport:
    path: str
    size_bytes: int
    file_sha256: str
    user_version: int
    component_versions: dict[str, int]
    tables: tuple[str, ...]
    integrity: str
    task_count: int
    verified_tasks: int
    compatible: bool
    issues: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class BackupManifest:
    schema: str
    backup_filename: str
    size_bytes: int
    file_sha256: str
    logical_sha256: str
    created_at: str
    user_version: int
    component_versions: dict[str, int]
    task_count: int
    verified_tasks: int
    signature_algorithm: str
    signature: str = field(repr=False)

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class SignedBackupResult:
    database: DatabaseReport
    manifest_path: str
    manifest: BackupManifest

    def as_dict(self) -> dict[str, object]:
        return {
            "database": self.database.as_dict(),
            "manifest_path": self.manifest_path,
            "manifest": self.manifest.as_dict(),
        }


@dataclass(frozen=True, slots=True)
class RestoreRehearsal:
    status: str
    backup_path: str
    manifest_path: str
    file_sha256: str
    logical_sha256: str
    restored_size_bytes: int
    user_version: int
    component_versions: dict[str, int]
    task_count: int
    verified_tasks: int

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def inspect_database(path: str | Path) -> DatabaseReport:
    database = Path(path)
    if not database.is_file():
        raise ValueError("database path must be a regular file")
    resolved = database.resolve()
    try:
        connection = sqlite3.connect(f"{resolved.as_uri()}?mode=ro", uri=True, timeout=5)
        connection.row_factory = sqlite3.Row
        try:
            integrity_rows = connection.execute("PRAGMA quick_check").fetchall()
            integrity = "; ".join(str(row[0]) for row in integrity_rows)
            user_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            tables = tuple(sorted(
                str(row[0]) for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                )
            ))
            component_versions: dict[str, int] = {}
            if "tengate_component_versions" in tables:
                component_versions = {
                    str(row[0]): int(row[1])
                    for row in connection.execute(
                        "SELECT component,version FROM tengate_component_versions ORDER BY component"
                    )
                }
            foreign_key_rows = connection.execute("PRAGMA foreign_key_check").fetchall()
            task_count, verified_tasks = _verify_application_integrity(connection, set(tables))
        finally:
            connection.close()
    except sqlite3.DatabaseError as exc:
        raise IntegrityFailure("database could not be inspected as SQLite") from exc

    issues: list[str] = []
    if integrity != "ok":
        issues.append(f"SQLite quick_check failed: {integrity}")
    if foreign_key_rows:
        issues.append(f"foreign key violations: {len(foreign_key_rows)}")
    if user_version > _SUPPORTED_USER_VERSION:
        issues.append(
            f"database schema {user_version} is newer than supported schema {_SUPPORTED_USER_VERSION}"
        )
    elif user_version < _SUPPORTED_USER_VERSION:
        issues.append(
            f"database schema {user_version} requires migration to {_SUPPORTED_USER_VERSION}"
        )
    missing_tables = sorted(_REQUIRED_TABLES - set(tables))
    if missing_tables:
        issues.append(f"missing required tables: {', '.join(missing_tables)}")
    if verified_tasks != task_count:
        issues.append(f"application integrity failed for {task_count - verified_tasks} of {task_count} tasks")
    for component, supported in _SUPPORTED_COMPONENTS.items():
        installed = component_versions.get(component)
        if installed is None:
            issues.append(f"missing component version: {component}")
        elif installed > supported:
            issues.append(f"component {component} version {installed} is newer than supported {supported}")
        elif installed < supported:
            issues.append(f"component {component} version {installed} requires migration to {supported}")
    return DatabaseReport(
        path=str(resolved),
        size_bytes=resolved.stat().st_size,
        file_sha256=_file_sha256(resolved),
        user_version=user_version,
        component_versions=component_versions,
        tables=tables,
        integrity=integrity,
        task_count=task_count,
        verified_tasks=verified_tasks,
        compatible=not issues,
        issues=tuple(issues),
    )


def create_backup(
    source: str | Path,
    destination: str | Path,
    *,
    progress: Callable[[int, int, int], None] | None = None,
) -> DatabaseReport:
    source_path = Path(source)
    destination_path = Path(destination)
    if not source_path.is_file():
        raise ValueError("source database path must be a regular file")
    if source_path.resolve() == destination_path.resolve():
        raise ValueError("backup destination must differ from the source database")
    if destination_path.exists():
        raise FileExistsError("backup destination already exists")
    if not destination_path.parent.is_dir():
        raise ValueError("backup destination parent must be an existing directory")

    temporary_handle = tempfile.NamedTemporaryFile(
        prefix=f".{destination_path.name}.", suffix=".tmp", dir=destination_path.parent, delete=False
    )
    temporary_path = Path(temporary_handle.name)
    temporary_handle.close()
    try:
        source_connection = sqlite3.connect(
            f"{source_path.resolve().as_uri()}?mode=ro", uri=True, timeout=5
        )
        target_connection = sqlite3.connect(temporary_path)
        try:
            source_connection.backup(
                target_connection, pages=16, progress=progress, sleep=0.01
            )
            target_connection.commit()
        finally:
            target_connection.close()
            source_connection.close()
        report = inspect_database(temporary_path)
        if not report.compatible:
            raise IntegrityFailure("backup failed compatibility preflight")
        os.link(temporary_path, destination_path)
        temporary_path.unlink()
        return inspect_database(destination_path)
    finally:
        temporary_path.unlink(missing_ok=True)


def create_signed_backup(
    source: str | Path,
    destination: str | Path,
    manifest_path: str | Path,
    key: bytes,
) -> SignedBackupResult:
    _validate_key(key)
    destination_path = Path(destination)
    manifest_destination = Path(manifest_path)
    if manifest_destination.exists():
        raise FileExistsError("backup manifest destination already exists")
    if manifest_destination.resolve() == destination_path.resolve():
        raise ValueError("backup and manifest destinations must differ")
    if not manifest_destination.parent.is_dir():
        raise ValueError("backup manifest parent must be an existing directory")
    report = create_backup(source, destination_path)
    unsigned: dict[str, object] = {
        "schema": _MANIFEST_SCHEMA,
        "backup_filename": destination_path.name,
        "size_bytes": report.size_bytes,
        "file_sha256": report.file_sha256,
        "logical_sha256": logical_sha256(destination_path),
        "created_at": datetime.now(UTC).isoformat(),
        "user_version": report.user_version,
        "component_versions": report.component_versions,
        "task_count": report.task_count,
        "verified_tasks": report.verified_tasks,
        "signature_algorithm": "hmac-sha256",
    }
    signature = hmac.new(key, _canonical(unsigned), hashlib.sha256).hexdigest()
    manifest = BackupManifest(**unsigned, signature=signature)
    with manifest_destination.open("x", encoding="utf-8") as handle:
        json.dump(manifest.as_dict(), handle, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        handle.write("\n")
    return SignedBackupResult(report, str(manifest_destination.resolve()), manifest)


def verify_backup_manifest(
    backup: str | Path, manifest_path: str | Path, key: bytes
) -> BackupManifest:
    _validate_key(key)
    backup_path = Path(backup)
    if not backup_path.is_file():
        raise ValueError("backup path must be a regular file")
    manifest_file = Path(manifest_path)
    if not manifest_file.is_file() or manifest_file.stat().st_size > 65_536:
        raise ValueError("manifest path must be a JSON file no larger than 64 KiB")
    try:
        raw = json.loads(
            manifest_file.read_text(encoding="utf-8"), object_pairs_hook=_unique_object
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise IntegrityFailure("backup manifest must be valid UTF-8 JSON") from exc
    manifest = _parse_manifest(raw)
    unsigned = manifest.as_dict()
    signature = str(unsigned.pop("signature"))
    expected = hmac.new(key, _canonical(unsigned), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, expected):
        raise IntegrityFailure("backup manifest signature is invalid")
    if manifest.backup_filename != backup_path.name:
        raise IntegrityFailure("backup filename does not match the signed manifest")
    stat = backup_path.stat()
    if manifest.size_bytes != stat.st_size:
        raise IntegrityFailure("backup size does not match the signed manifest")
    if not hmac.compare_digest(manifest.file_sha256, _file_sha256(backup_path)):
        raise IntegrityFailure("backup file digest does not match the signed manifest")
    if not hmac.compare_digest(manifest.logical_sha256, logical_sha256(backup_path)):
        raise IntegrityFailure("backup logical digest does not match the signed manifest")
    report = inspect_database(backup_path)
    if not report.compatible:
        raise IntegrityFailure("backup database is not compatible with this runtime")
    if report.user_version != manifest.user_version:
        raise IntegrityFailure("backup schema version does not match the signed manifest")
    if report.component_versions != manifest.component_versions:
        raise IntegrityFailure("backup component versions do not match the signed manifest")
    if report.task_count != manifest.task_count or report.verified_tasks != manifest.verified_tasks:
        raise IntegrityFailure("backup task integrity counts do not match the signed manifest")
    return manifest


def rehearse_restore(
    backup: str | Path, manifest_path: str | Path, key: bytes
) -> RestoreRehearsal:
    backup_path = Path(backup)
    manifest_file = Path(manifest_path)
    manifest = verify_backup_manifest(backup_path, manifest_file, key)
    with tempfile.TemporaryDirectory() as directory:
        restored = Path(directory) / "restored.db"
        _copy_database(backup_path, restored)
        report = inspect_database(restored)
        restored_logical = logical_sha256(restored)
        if not report.compatible or not hmac.compare_digest(restored_logical, manifest.logical_sha256):
            raise IntegrityFailure("isolated restore did not reproduce the signed logical database")
        return RestoreRehearsal(
            status="verified",
            backup_path=str(backup_path.resolve()),
            manifest_path=str(manifest_file.resolve()),
            file_sha256=manifest.file_sha256,
            logical_sha256=restored_logical,
            restored_size_bytes=report.size_bytes,
            user_version=report.user_version,
            component_versions=report.component_versions,
            task_count=report.task_count,
            verified_tasks=report.verified_tasks,
        )


def logical_sha256(path: str | Path) -> str:
    database = Path(path)
    if not database.is_file():
        raise ValueError("database path must be a regular file")
    try:
        connection = sqlite3.connect(f"{database.resolve().as_uri()}?mode=ro", uri=True, timeout=5)
        try:
            checksum = hashlib.sha256()
            user_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            checksum.update(f"user_version={user_version}\n".encode())
            for line in connection.iterdump():
                checksum.update(line.encode("utf-8"))
                checksum.update(b"\n")
            return checksum.hexdigest()
        finally:
            connection.close()
    except sqlite3.DatabaseError as exc:
        raise IntegrityFailure("database logical digest could not be computed") from exc


def _copy_database(source: Path, destination: Path) -> None:
    source_connection = sqlite3.connect(f"{source.resolve().as_uri()}?mode=ro", uri=True, timeout=5)
    target_connection = sqlite3.connect(destination)
    try:
        source_connection.backup(target_connection)
        target_connection.commit()
    finally:
        target_connection.close()
        source_connection.close()


def _parse_manifest(value: object) -> BackupManifest:
    if not isinstance(value, dict):
        raise IntegrityFailure("backup manifest must be an object")
    expected = {
        "schema", "backup_filename", "size_bytes", "file_sha256", "logical_sha256",
        "created_at", "user_version", "component_versions", "task_count", "verified_tasks",
        "signature_algorithm", "signature",
    }
    if set(value) != expected:
        raise IntegrityFailure("backup manifest keys do not match schema")
    if value["schema"] != _MANIFEST_SCHEMA or value["signature_algorithm"] != "hmac-sha256":
        raise IntegrityFailure("backup manifest schema or signature algorithm is unsupported")
    filename = value["backup_filename"]
    if (
        not isinstance(filename, str) or not filename or filename in {".", ".."}
        or Path(filename).name != filename or "\\" in filename
    ):
        raise IntegrityFailure("backup manifest filename is invalid")
    size = value["size_bytes"]
    user_version = value["user_version"]
    task_count = value["task_count"]
    verified_tasks = value["verified_tasks"]
    if isinstance(size, bool) or not isinstance(size, int) or size < 1:
        raise IntegrityFailure("backup manifest size is invalid")
    if isinstance(user_version, bool) or not isinstance(user_version, int) or user_version < 0:
        raise IntegrityFailure("backup manifest schema version is invalid")
    if (
        isinstance(task_count, bool) or not isinstance(task_count, int) or task_count < 0
        or isinstance(verified_tasks, bool) or not isinstance(verified_tasks, int)
        or not 0 <= verified_tasks <= task_count
    ):
        raise IntegrityFailure("backup manifest task integrity counts are invalid")
    for name in ("file_sha256", "logical_sha256", "signature"):
        if not isinstance(value[name], str) or not _SHA256.fullmatch(value[name]):
            raise IntegrityFailure(f"backup manifest {name} is invalid")
    created_at = value["created_at"]
    try:
        parsed_time = datetime.fromisoformat(created_at) if isinstance(created_at, str) else None
    except ValueError as exc:
        raise IntegrityFailure("backup manifest timestamp is invalid") from exc
    if parsed_time is None or parsed_time.tzinfo is None:
        raise IntegrityFailure("backup manifest timestamp must include a timezone")
    components = value["component_versions"]
    if not isinstance(components, dict) or any(
        not isinstance(name, str) or not name
        or isinstance(version, bool) or not isinstance(version, int) or version < 0
        for name, version in components.items()
    ):
        raise IntegrityFailure("backup manifest component versions are invalid")
    return BackupManifest(
        schema=str(value["schema"]),
        backup_filename=filename,
        size_bytes=size,
        file_sha256=str(value["file_sha256"]),
        logical_sha256=str(value["logical_sha256"]),
        created_at=created_at,
        user_version=user_version,
        component_versions={str(name): int(version) for name, version in components.items()},
        task_count=task_count,
        verified_tasks=verified_tasks,
        signature_algorithm=str(value["signature_algorithm"]),
        signature=str(value["signature"]),
    )


def _validate_key(key: bytes) -> None:
    if not isinstance(key, bytes) or len(key) < 32:
        raise ValueError("backup manifest key must contain at least 32 bytes")


def _verify_application_integrity(
    connection: sqlite3.Connection, tables: set[str]
) -> tuple[int, int]:
    required = {"tasks", "task_events", "evidence", "checkpoints"}
    if not required <= tables:
        return 0, 0
    tasks = connection.execute(
        "SELECT task_id,state,revision,last_event_hash FROM tasks ORDER BY task_id"
    ).fetchall()
    verified = sum(1 for task in tasks if _verify_task(connection, task))
    return len(tasks), verified


def _verify_task(connection: sqlite3.Connection, task: sqlite3.Row) -> bool:
    task_id = str(task["task_id"])
    rows = connection.execute(
        "SELECT * FROM task_events WHERE task_id=? ORDER BY revision", (task_id,)
    ).fetchall()
    previous = "0" * 64
    try:
        for expected_revision, row in enumerate(rows):
            if int(row["revision"]) != expected_revision:
                return False
            details = json.loads(str(row["details_json"]))
            expected = digest({
                "event_id": row["event_id"],
                "task_id": task_id,
                "revision": row["revision"],
                "action": row["action"],
                "actor": row["actor_subject"],
                "issuer": row["actor_issuer"],
                "details": details,
                "previous_hash": previous,
                "occurred_at": row["occurred_at"],
            })
            if row["previous_hash"] != previous or row["event_hash"] != expected:
                return False
            previous = str(row["event_hash"])
    except (json.JSONDecodeError, TypeError, ValueError):
        return False
    if (
        not rows
        or int(task["revision"]) != len(rows) - 1
        or task["last_event_hash"] != previous
    ):
        return False

    evidence_rows = connection.execute(
        "SELECT * FROM evidence WHERE task_id=? ORDER BY evidence_id", (task_id,)
    ).fetchall()
    for row in evidence_rows:
        if row["evidence_id"] != digest([
            row["task_id"], row["kind"], row["immutable_ref"], row["sha256"],
            row["verifier"], row["created_at"],
        ]):
            return False
    evidence_ids = {str(row["evidence_id"]) for row in evidence_rows}
    checkpoint_rows = connection.execute(
        "SELECT * FROM checkpoints WHERE task_id=? ORDER BY checkpoint_id", (task_id,)
    ).fetchall()
    for row in checkpoint_rows:
        if row["checkpoint_id"] != digest([
            row["task_id"], row["revision"], row["summary"], row["next_action"],
            row["blocked_on"], row["last_evidence"], row["created_at"],
        ]):
            return False
        if row["last_evidence"] and str(row["last_evidence"]) not in evidence_ids:
            return False
    if str(task["state"]) in {"review", "accepted"} and not evidence_rows:
        return False
    return True


def _canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise IntegrityFailure(f"duplicate backup manifest key: {key}")
        result[key] = value
    return result


def _file_sha256(path: Path) -> str:
    checksum = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            checksum.update(chunk)
    return checksum.hexdigest()

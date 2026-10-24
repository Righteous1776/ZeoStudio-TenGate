from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping


_ENV_NAME = re.compile(r"^[A-Z_][A-Z0-9_]{0,127}$")
_NAME = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_LOGIN = re.compile(r"^[A-Za-z0-9-]{1,100}$")
_EVENT_RULE = re.compile(r"^[a-z0-9_]{1,80}:(?:\*|[a-z0-9_]{1,80})$")
_TASK_PREFIX = re.compile(r"^[A-Z][A-Z0-9-]{1,30}$")


@dataclass(frozen=True, slots=True)
class BindingConfig:
    repository: str
    login: str
    roles: frozenset[str]
    lane: str | None
    event_rules: frozenset[str]
    seal_ttl_seconds: int


@dataclass(frozen=True, slots=True)
class IdentityConfig:
    issuer: str
    seal_key_env: str


@dataclass(frozen=True, slots=True)
class GitHubConfig:
    webhook_secret_env: str
    max_body_bytes: int
    max_attempts: int
    bindings: tuple[BindingConfig, ...]


@dataclass(frozen=True, slots=True)
class WorkerConfig:
    name: str
    lease_seconds: int
    poll_interval_ms: int


@dataclass(frozen=True, slots=True)
class BridgeConfig:
    task_prefix: str
    routes: frozenset[str]


@dataclass(frozen=True, slots=True)
class BackupConfig:
    manifest_key_env: str


@dataclass(frozen=True, slots=True)
class TenGateConfig:
    schema_version: str
    database: Path
    identity: IdentityConfig
    github: GitHubConfig
    worker: WorkerConfig
    bridge: BridgeConfig
    backup: BackupConfig | None = None


@dataclass(frozen=True, slots=True)
class ResolvedSecrets:
    seal_key: bytes = field(repr=False)
    webhook_secret: bytes = field(repr=False)


def load_config(path: str | Path) -> TenGateConfig:
    config_path = Path(path)
    if not config_path.is_file():
        raise ValueError("configuration path must be a regular file")
    if config_path.stat().st_size > 1_048_576:
        raise ValueError("configuration file exceeds 1 MiB")
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"), object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("configuration must be valid UTF-8 JSON") from exc
    root = _object(raw, "config")
    _keys(
        root,
        "config",
        {"schema_version", "database", "identity", "github", "worker", "bridge"},
        {"backup"},
    )
    schema_version = _string(root, "schema_version", "config")
    if schema_version != "1":
        raise ValueError(f"unsupported configuration schema: {schema_version}")
    database_text = _string(root, "database", "config")
    database = Path(database_text)
    if database_text != ":memory:" and not database.is_absolute():
        database = (config_path.parent / database).resolve()

    identity_raw = _object(root.get("identity"), "identity")
    _keys(identity_raw, "identity", {"issuer", "seal_key_env"})
    identity = IdentityConfig(
        issuer=_named(_string(identity_raw, "issuer", "identity"), "identity.issuer"),
        seal_key_env=_environment(_string(identity_raw, "seal_key_env", "identity"), "identity.seal_key_env"),
    )

    github_raw = _object(root.get("github"), "github")
    _keys(github_raw, "github", {"webhook_secret_env", "max_body_bytes", "max_attempts", "bindings"})
    binding_items = github_raw.get("bindings")
    if not isinstance(binding_items, list) or not binding_items:
        raise ValueError("github.bindings must be a non-empty array")
    bindings: list[BindingConfig] = []
    seen: set[tuple[str, str]] = set()
    for index, item in enumerate(binding_items):
        label = f"github.bindings[{index}]"
        binding_raw = _object(item, label)
        _keys(binding_raw, label, {"repository", "login", "roles", "lane", "event_rules", "seal_ttl_seconds"})
        repository = _string(binding_raw, "repository", label)
        login = _string(binding_raw, "login", label)
        if not _REPOSITORY.fullmatch(repository):
            raise ValueError(f"{label}.repository is invalid")
        if not _LOGIN.fullmatch(login):
            raise ValueError(f"{label}.login is invalid")
        key = (repository.casefold(), login.casefold())
        if key in seen:
            raise ValueError("github bindings must use unique repository/login pairs")
        seen.add(key)
        roles = _string_set(binding_raw.get("roles"), f"{label}.roles", _NAME)
        rules = _string_set(binding_raw.get("event_rules"), f"{label}.event_rules", _EVENT_RULE)
        lane_raw = binding_raw.get("lane")
        lane = None if lane_raw is None else _named_value(lane_raw, f"{label}.lane")
        bindings.append(BindingConfig(
            repository=repository,
            login=login,
            roles=roles,
            lane=lane,
            event_rules=rules,
            seal_ttl_seconds=_integer(binding_raw, "seal_ttl_seconds", label, 1, 86_400),
        ))
    github = GitHubConfig(
        webhook_secret_env=_environment(
            _string(github_raw, "webhook_secret_env", "github"), "github.webhook_secret_env"
        ),
        max_body_bytes=_integer(github_raw, "max_body_bytes", "github", 1, 10_485_760),
        max_attempts=_integer(github_raw, "max_attempts", "github", 1, 1000),
        bindings=tuple(bindings),
    )

    worker_raw = _object(root.get("worker"), "worker")
    _keys(worker_raw, "worker", {"name", "lease_seconds"}, {"poll_interval_ms"})
    worker = WorkerConfig(
        name=_named(_string(worker_raw, "name", "worker"), "worker.name"),
        lease_seconds=_integer(worker_raw, "lease_seconds", "worker", 1, 86_400),
        poll_interval_ms=(
            _integer(worker_raw, "poll_interval_ms", "worker", 10, 60_000)
            if "poll_interval_ms" in worker_raw else 250
        ),
    )

    bridge_raw = _object(root.get("bridge"), "bridge")
    _keys(bridge_raw, "bridge", {"task_prefix", "routes"})
    task_prefix = _string(bridge_raw, "task_prefix", "bridge")
    if not _TASK_PREFIX.fullmatch(task_prefix):
        raise ValueError("bridge.task_prefix is invalid")
    routes = _string_set(bridge_raw.get("routes"), "bridge.routes", _EVENT_RULE)
    backup = None
    if "backup" in root:
        backup_raw = _object(root.get("backup"), "backup")
        _keys(backup_raw, "backup", {"manifest_key_env"})
        backup = BackupConfig(manifest_key_env=_environment(
            _string(backup_raw, "manifest_key_env", "backup"), "backup.manifest_key_env"
        ))
    return TenGateConfig(
        schema_version=schema_version,
        database=database,
        identity=identity,
        github=github,
        worker=worker,
        bridge=BridgeConfig(task_prefix=task_prefix, routes=routes),
        backup=backup,
    )


def resolve_secrets(config: TenGateConfig, environ: Mapping[str, str]) -> ResolvedSecrets:
    seal = environ.get(config.identity.seal_key_env)
    webhook = environ.get(config.github.webhook_secret_env)
    missing = [
        name for name, value in (
            (config.identity.seal_key_env, seal),
            (config.github.webhook_secret_env, webhook),
        ) if value is None
    ]
    if missing:
        raise ValueError(f"required secret environment variables are missing: {', '.join(missing)}")
    assert seal is not None and webhook is not None
    seal_bytes = seal.encode("utf-8")
    webhook_bytes = webhook.encode("utf-8")
    if len(seal_bytes) < 32 or len(webhook_bytes) < 32:
        raise ValueError("secret environment values must contain at least 32 UTF-8 bytes")
    return ResolvedSecrets(seal_key=seal_bytes, webhook_secret=webhook_bytes)


def resolve_backup_key(config: TenGateConfig, environ: Mapping[str, str]) -> bytes:
    if config.backup is None:
        raise ValueError("backup.manifest_key_env must be configured")
    value = environ.get(config.backup.manifest_key_env)
    if value is None:
        raise ValueError(f"required secret environment variable is missing: {config.backup.manifest_key_env}")
    key = value.encode("utf-8")
    if len(key) < 32:
        raise ValueError("backup manifest key must contain at least 32 UTF-8 bytes")
    return key


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate configuration key: {key}")
        result[key] = value
    return result


def _object(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    return value


def _keys(
    value: Mapping[str, object],
    label: str,
    required: set[str],
    optional: set[str] | None = None,
) -> None:
    allowed = required | (optional or set())
    missing = required - value.keys()
    unknown = value.keys() - allowed
    if missing:
        raise ValueError(f"{label} is missing keys: {', '.join(sorted(missing))}")
    if unknown:
        raise ValueError(f"{label} contains unknown keys: {', '.join(sorted(unknown))}")


def _string(value: Mapping[str, object], key: str, label: str) -> str:
    item = value.get(key)
    if not isinstance(item, str) or not item:
        raise ValueError(f"{label}.{key} must be a non-empty string")
    return item


def _integer(value: Mapping[str, object], key: str, label: str, minimum: int, maximum: int) -> int:
    item = value.get(key)
    if isinstance(item, bool) or not isinstance(item, int) or not minimum <= item <= maximum:
        raise ValueError(f"{label}.{key} must be an integer between {minimum} and {maximum}")
    return item


def _string_set(value: object, label: str, pattern: re.Pattern[str]) -> frozenset[str]:
    if not isinstance(value, list) or not value:
        raise ValueError(f"{label} must be a non-empty array")
    if any(not isinstance(item, str) or not pattern.fullmatch(item) for item in value):
        raise ValueError(f"{label} contains an invalid value")
    if len(set(value)) != len(value):
        raise ValueError(f"{label} must not contain duplicates")
    return frozenset(value)


def _environment(value: str, label: str) -> str:
    if not _ENV_NAME.fullmatch(value):
        raise ValueError(f"{label} must be an uppercase environment variable name")
    return value


def _named(value: str, label: str) -> str:
    if not _NAME.fullmatch(value):
        raise ValueError(f"{label} is invalid")
    return value


def _named_value(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be a string or null")
    return _named(value, label)


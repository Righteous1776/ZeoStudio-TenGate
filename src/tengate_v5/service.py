from __future__ import annotations

import threading
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from enum import StrEnum

from .github_bridge import GitHubTaskBridge


class WorkerState(StrEnum):
    CREATED = "created"
    RUNNING = "running"
    STOPPING = "stopping"
    STOPPED = "stopped"


@dataclass(frozen=True, slots=True)
class WorkerSnapshot:
    state: WorkerState
    processed: int
    errors: int
    last_error_type: str | None
    started_at: str | None
    last_activity_at: str | None

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


class BridgeWorker:
    """Single background consumer with observable lifecycle and bounded shutdown."""

    def __init__(
        self,
        bridge: GitHubTaskBridge,
        worker_name: str,
        *,
        lease_seconds: int = 60,
        poll_interval_ms: int = 250,
    ) -> None:
        if not worker_name or len(worker_name) > 200:
            raise ValueError("worker_name must contain 1 to 200 characters")
        if not 1 <= lease_seconds <= 86_400:
            raise ValueError("lease_seconds must be between 1 and 86400")
        if not 10 <= poll_interval_ms <= 60_000:
            raise ValueError("poll_interval_ms must be between 10 and 60000")
        self._bridge = bridge
        self._worker_name = worker_name
        self._lease_seconds = lease_seconds
        self._poll_seconds = poll_interval_ms / 1000
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._state = WorkerState.CREATED
        self._processed = 0
        self._errors = 0
        self._last_error_type: str | None = None
        self._started_at: str | None = None
        self._last_activity_at: str | None = None

    def start(self) -> bool:
        with self._lock:
            if self._state == WorkerState.RUNNING:
                return False
            if self._state != WorkerState.CREATED:
                raise RuntimeError("a stopped worker cannot be restarted")
            now = self._now()
            self._state = WorkerState.RUNNING
            self._started_at = now
            self._thread = threading.Thread(
                target=self._run,
                name=f"tengate-bridge-{self._worker_name}",
                daemon=True,
            )
            self._thread.start()
            return True

    def stop(self, timeout: float = 10.0) -> bool:
        if timeout < 0:
            raise ValueError("timeout must not be negative")
        with self._lock:
            if self._state == WorkerState.CREATED:
                self._state = WorkerState.STOPPED
                return True
            if self._state == WorkerState.STOPPED:
                return True
            self._state = WorkerState.STOPPING
            thread = self._thread
            self._stop.set()
        if thread is not None:
            thread.join(timeout)
        stopped = thread is None or not thread.is_alive()
        if stopped:
            with self._lock:
                self._state = WorkerState.STOPPED
        return stopped

    def snapshot(self) -> WorkerSnapshot:
        with self._lock:
            return WorkerSnapshot(
                state=self._state,
                processed=self._processed,
                errors=self._errors,
                last_error_type=self._last_error_type,
                started_at=self._started_at,
                last_activity_at=self._last_activity_at,
            )

    def _run(self) -> None:
        try:
            while not self._stop.is_set():
                try:
                    result = self._bridge.process_one(
                        self._worker_name, lease_seconds=self._lease_seconds
                    )
                except Exception as exc:
                    with self._lock:
                        self._errors += 1
                        self._last_error_type = type(exc).__name__
                        self._last_activity_at = self._now()
                    self._stop.wait(self._poll_seconds)
                    continue
                if result is None:
                    self._stop.wait(self._poll_seconds)
                    continue
                with self._lock:
                    self._processed += 1
                    self._last_activity_at = self._now()
        finally:
            with self._lock:
                self._state = WorkerState.STOPPED

    @staticmethod
    def _now() -> str:
        return datetime.now(UTC).isoformat()

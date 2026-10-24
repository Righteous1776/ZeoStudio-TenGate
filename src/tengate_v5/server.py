from __future__ import annotations

import json
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Mapping

from .errors import IntegrityFailure, TenGateError, WebhookRejected
from .runtime import RuntimeStack
from .service import BridgeWorker, WorkerState


class WebhookApplication:
    """Small HTTP-independent boundary for health and signed webhook intake."""

    def __init__(self, runtime: RuntimeStack, worker: BridgeWorker | None = None) -> None:
        self._runtime = runtime
        self._worker = worker

    def health(self) -> tuple[int, dict[str, object]]:
        payload: dict[str, object] = {
            "status": "ok",
            "service": "tengate-v5",
            "inbox": self._runtime.inbox.stats(),
        }
        if self._worker is not None:
            payload["worker"] = self._worker.snapshot().as_dict()
        return HTTPStatus.OK, payload

    def ready(self) -> tuple[int, dict[str, object]]:
        if self._worker is None:
            return HTTPStatus.OK, {"status": "ready", "mode": "intake-only"}
        worker = self._worker.snapshot()
        ready = worker.state == WorkerState.RUNNING
        return (HTTPStatus.OK if ready else HTTPStatus.SERVICE_UNAVAILABLE), {
            "status": "ready" if ready else "not_ready",
            "mode": "intake-and-process",
            "worker": worker.as_dict(),
        }

    def receive(
        self, headers: Mapping[str, str], body: bytes
    ) -> tuple[int, dict[str, object]]:
        event = self._runtime.adapter.verify(headers, body)
        registered = self._runtime.inbox.register(event)
        return HTTPStatus.ACCEPTED, {
            "status": "received",
            "delivery_id": event.delivery_id,
            "registered": registered,
            "resource_ref": event.resource_ref,
        }


class TenGateHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def create_server(
    runtime: RuntimeStack,
    host: str = "127.0.0.1",
    port: int = 8080,
    *,
    worker: BridgeWorker | None = None,
) -> TenGateHTTPServer:
    if not host or not 0 <= port <= 65_535:
        raise ValueError("host and port must identify a valid listening address")
    app = WebhookApplication(runtime, worker)
    max_body_bytes = runtime.config.github.max_body_bytes

    class Handler(BaseHTTPRequestHandler):
        server_version = "TenGateV5"
        sys_version = ""

        def do_GET(self) -> None:
            if self.path == "/healthz":
                status, payload = app.health()
            elif self.path == "/readyz":
                status, payload = app.ready()
            else:
                self._json(HTTPStatus.NOT_FOUND, {"status": "not_found"})
                return
            self._json(status, payload)

        def do_POST(self) -> None:
            if self.path != "/github/webhook":
                self._json(HTTPStatus.NOT_FOUND, {"status": "not_found"})
                return
            try:
                headers = self._webhook_headers()
                body = self._body(max_body_bytes)
                status, payload = app.receive(headers, body)
            except _RequestRejected as exc:
                self._json(exc.status, {"status": "rejected", "message": exc.message})
                return
            except WebhookRejected as exc:
                self._json(HTTPStatus.UNAUTHORIZED, {"status": "rejected", "message": str(exc)})
                return
            except IntegrityFailure as exc:
                self._json(HTTPStatus.CONFLICT, {"status": "rejected", "message": str(exc)})
                return
            except TenGateError:
                self._json(HTTPStatus.BAD_REQUEST, {"status": "rejected"})
                return
            except Exception:
                self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"status": "error"})
                return
            self._json(status, payload)

        def _webhook_headers(self) -> dict[str, str]:
            if self.headers.get_all("Transfer-Encoding"):
                raise _RequestRejected(HTTPStatus.BAD_REQUEST, "transfer encoding is not supported")
            result: dict[str, str] = {}
            for name in (
                "Content-Type",
                "X-GitHub-Delivery",
                "X-GitHub-Event",
                "X-Hub-Signature-256",
            ):
                values = self.headers.get_all(name, failobj=[])
                if len(values) != 1:
                    raise _RequestRejected(HTTPStatus.BAD_REQUEST, f"exactly one {name} header is required")
                result[name] = values[0]
            return result

        def _body(self, maximum: int) -> bytes:
            values = self.headers.get_all("Content-Length", failobj=[])
            if len(values) != 1:
                raise _RequestRejected(HTTPStatus.LENGTH_REQUIRED, "exactly one Content-Length is required")
            try:
                length = int(values[0], 10)
            except ValueError as exc:
                raise _RequestRejected(HTTPStatus.BAD_REQUEST, "Content-Length must be an integer") from exc
            if length < 1 or length > maximum:
                raise _RequestRejected(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "request body exceeds the limit")
            body = self.rfile.read(length)
            if len(body) != length:
                raise _RequestRejected(HTTPStatus.BAD_REQUEST, "request body is incomplete")
            return body

        def _json(self, status: int, payload: Mapping[str, object]) -> None:
            body = (json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            return

    return TenGateHTTPServer((host, port), Handler)


class _RequestRejected(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message

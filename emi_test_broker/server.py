# SPDX-FileCopyrightText: 2026 Aleksandr Polskiy
# SPDX-License-Identifier: Apache-2.0
"""The broker's HTTP front: stdlib ``ThreadingHTTPServer`` on loopback, token-protected (design §7).

``GET /health`` needs no token (it carries no secret). Every ``POST`` needs the per-start token from the
state file in the ``X-EMI-Broker-Token`` header. Binding the port is the lock: a second broker cannot bind
it, so at most one broker serves a port.
"""

import hmac
import json
import logging
import os
import secrets
import socket
import threading
from collections.abc import Callable
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from emi_greenmail.errors import GreenmailError
from emi_reader import ReaderError
from emi_sender import SenderError
from emi_test_broker.broker_log import LOGGER_NAME, configure, log_port_unavailable
from emi_test_broker.codec import error_to_json
from emi_test_broker.errors import BrokerUnavailableError, TestBrokerError
from emi_test_broker.keywords import TestBrokerKeyword
from emi_test_broker.service import BrokerService
from emi_test_broker.settings import BrokerState, TOKEN_HEADER

_LOG = logging.getLogger(LOGGER_NAME)
_MAX_BODY_BYTES = 1_000_000
# Errors that describe the request's outcome (sent back typed, status 422), not a broker fault.
_DOMAIN_ERRORS = (ReaderError, SenderError, TestBrokerError, GreenmailError, ValueError, TypeError)


class BrokerHttpServer(ThreadingHTTPServer):
    """Threading HTTP server that refuses to share its port (``SO_EXCLUSIVEADDRUSE`` on Windows)."""

    daemon_threads = True
    # POSIX SO_REUSEADDR only skips TIME_WAIT (a quick restart); on Windows it would let a second broker
    # bind the same port, so there the socket is made exclusive instead.
    allow_reuse_address = os.name != "nt"

    def __init__(self, address: tuple[str, int], service: BrokerService, token: str) -> None:
        """Bind and listen.

        Args:
            address: ``(host, port)``.
            service: Request handling.
            token: Access token clients must send.
        """
        self.service = service
        self.token = token
        super().__init__(address, BrokerHandler)

    def server_bind(self) -> None:
        """Bind exclusively."""
        exclusive = getattr(socket, "SO_EXCLUSIVEADDRUSE", None)
        if exclusive is not None:
            self.socket.setsockopt(socket.SOL_SOCKET, exclusive, 1)
        self.socket.bind(self.server_address)
        self.server_address = self.socket.getsockname()[:2]
        self.server_name, self.server_port = str(self.server_address[0]), int(self.server_address[1])


class BrokerHandler(BaseHTTPRequestHandler):
    """Routes requests to :class:`BrokerService`."""

    server: BrokerHttpServer
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:  # pylint: disable=invalid-name  # name fixed by BaseHTTPRequestHandler
        """Serve ``/health``; ``/inventory`` is not implemented yet."""
        self.server.service.touch()
        if self.path == "/health":
            self._reply(HTTPStatus.OK, self.server.service.health())
        elif self.path.startswith("/inventory"):
            self._reply(HTTPStatus.NOT_IMPLEMENTED, {"error": {"type": "NotImplementedError", "message":
                        "inventory arrives with the reader's inventory (design §5.8)"}})
        else:
            self._reply(HTTPStatus.NOT_FOUND, {"error": {"type": "NotFound", "message": self.path}})

    def do_POST(self) -> None:  # pylint: disable=invalid-name  # name fixed by BaseHTTPRequestHandler
        """Serve every state-changing endpoint."""
        service = self.server.service
        service.touch()
        sent = self.headers.get(TOKEN_HEADER, "")
        if not hmac.compare_digest(sent.encode(), self.server.token.encode()):
            self._reply(HTTPStatus.UNAUTHORIZED, {"error": {"type": "Unauthorized", "message": "bad or missing token"}})
            return
        payload = self._payload()
        if payload is None:
            return
        routes: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
            "/send": service.send,
            "/wait": service.wait,
            "/lease": lambda body: service.lease("acquire", body),
            "/lease/heartbeat": lambda body: service.lease("heartbeat", body),
            "/lease/release": lambda body: service.lease("release", body),
            "/purge": service.purge,
            "/shutdown": self._shutdown,
        }
        route = routes.get(self.path)
        if route is None:
            self._reply(HTTPStatus.NOT_FOUND, {"error": {"type": "NotFound", "message": self.path}})
            return
        try:
            self._reply(HTTPStatus.OK, route(payload))
        except _DOMAIN_ERRORS as error:
            self._reply(HTTPStatus.UNPROCESSABLE_ENTITY, {"error": error_to_json(error)})
        # The broker serves every run on the host: one bad request must not take it down.
        except Exception as error:  # pylint: disable=broad-exception-caught
            _LOG.exception("unexpected error on %s", self.path)
            self._reply(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": error_to_json(error)})

    def log_message(self, format: str, *args: Any) -> None:  # pylint: disable=redefined-builtin
        """Send access lines to the broker log at debug level instead of stderr."""
        _LOG.debug("%s %s", self.address_string(), format % args)

    def _payload(self) -> dict[str, Any] | None:
        """Read the JSON body; replies 400 and returns None when it is not a JSON object."""
        length = int(self.headers.get("Content-Length") or 0)
        if length > _MAX_BODY_BYTES:
            self._reply(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"error": {"type": "TooLarge", "message": str(length)}})
            return None
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, UnicodeDecodeError) as error:
            self._reply(HTTPStatus.BAD_REQUEST, {"error": {"type": "BadRequest", "message": str(error)}})
            return None
        if not isinstance(payload, dict):
            self._reply(HTTPStatus.BAD_REQUEST, {"error": {"type": "BadRequest", "message": "body must be an object"}})
            return None
        return payload

    def _shutdown(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Stop the server after answering."""
        _LOG.info("shutdown requested by %s", payload.get("requested_by", "client"))
        threading.Thread(target=self.server.shutdown, daemon=True).start()
        return {"stopping": True}

    def _reply(self, status: HTTPStatus, body: dict[str, Any]) -> None:
        """Send a JSON answer."""
        data = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def bind(service: BrokerService) -> tuple[BrokerHttpServer, BrokerState]:
    """Bind the broker's port and write its state file.

    Args:
        service: Request handling (carries the settings).

    Returns:
        The bound server and the state written for clients.

    Raises:
        BrokerUnavailableError: ``TEST_BROKER_PORT_UNAVAILABLE`` when the port cannot be opened (logged
            with a timestamp, kept for debug reports).
    """
    settings = service.settings
    token = secrets.token_urlsafe(32)
    try:
        server = BrokerHttpServer((settings.host, settings.port), service, token)
    except OSError as error:
        log_port_unavailable(settings.log_dir, settings.port, f"{type(error).__name__}: {error}")
        raise BrokerUnavailableError(
            f"Test broker could not open {settings.host}:{settings.port}: {error}",
            TestBrokerKeyword.PORT_UNAVAILABLE, infrastructure=True,
            details={"host": settings.host, "port": settings.port, "error": str(error)},
        ) from error
    state = BrokerState(pid=os.getpid(), port=settings.port, token=token, config_hash=settings.config_hash())
    state.save(settings.state_file)
    return server, state


def serve(server: BrokerHttpServer, state: BrokerState) -> None:
    """Serve until shut down (request, idle timeout); then clean up.

    Args:
        server: A bound server from :func:`bind`.
        state: Its state.
    """
    service = server.service
    settings = service.settings
    configure(settings.log_dir)
    stop = threading.Event()
    watchdog = threading.Thread(target=_idle_watchdog, args=(server, stop), daemon=True)
    watchdog.start()
    _LOG.info("broker %s started pid=%d port=%d config=%s settings=%s", state.version, state.pid, state.port,
              state.config_hash, json.dumps(settings.describe()))
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        stop.set()
        server.server_close()
        service.close()
        current = BrokerState.load(settings.state_file)
        if current is not None and current.pid == state.pid:
            settings.state_file.unlink(missing_ok=True)
        _LOG.info("broker stopped pid=%d", state.pid)


def _idle_watchdog(server: BrokerHttpServer, stop: threading.Event) -> None:
    """Shut the server down after the idle limit, unless a run still holds a lease."""
    service = server.service
    limit = service.settings.idle_shutdown_sec
    interval = max(0.05, min(30.0, limit / 10))
    while not stop.wait(interval):
        if service.idle_for() >= limit and not service.has_active_runs():
            _LOG.info("idle for %.0f s with no active runs: shutting down", service.idle_for())
            server.shutdown()
            return

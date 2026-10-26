# SPDX-FileCopyrightText: 2026 Aleksandr Polskiy
# SPDX-License-Identifier: Apache-2.0
"""Test doubles for the Greenmail client: a simulated REST API and simulated mail ports."""

import json

from emi_greenmail import GreenmailClient, GreenmailSettings
from unittests.sims import SimClock

READY_BODY = b'{"message":"Service running"}'


class SimGreenmail:
    """Simulated Greenmail: REST routes with scripted answers and a set of closed ports."""

    def __init__(self) -> None:
        """Start ready, with the ``qa`` user, every port open."""
        self.routes: dict[tuple[str, str], tuple[int, bytes]] = {
            ("GET", "/api/service/readiness"): (200, READY_BODY),
            ("GET", "/api/user"): (200, b'[{"login":"qa","email":"qa@example.com"}]'),
            ("GET", "/api/configuration"): (200, b'{"serverSetups":[{"port":3025,"protocol":"smtp"}]}'),
            ("POST", "/api/user"): (200, b'{"login":"qa2","email":"qa2@example.com"}'),
            ("POST", "/api/mail/purge"): (200, b'{"message":"Purged mails"}'),
        }
        self.closed_ports: set[int] = set()
        self.api_down = False
        self.calls: list[tuple[str, str, bytes | None, float]] = []
        self.ready_after_calls = 0

    def http(self, method: str, url: str, body: bytes | None, timeout: float) -> tuple[int, bytes]:
        """Answer one HTTP request."""
        self.calls.append((method, url, body, timeout))
        if self.api_down:
            raise ConnectionRefusedError(10061, "No connection could be made")
        path = "/" + url.split("/", 3)[3]
        if path == "/api/service/readiness" and len(self.calls) <= self.ready_after_calls:
            return 503, json.dumps({"message": "Service not ready"}).encode()
        if method == "DELETE" and path.startswith("/api/user/"):
            return self.routes.get((method, path), (200, b'{"message":"deleted"}'))
        return self.routes.get((method, path), (404, b'{"message":"Not found"}'))

    def connect(self, host: str, port: int, timeout: float) -> None:
        """Accept or refuse one TCP connection."""
        del host, timeout
        if port in self.closed_ports:
            raise ConnectionRefusedError(10061, "No connection could be made")

    def client(self, clock: SimClock | None = None) -> GreenmailClient:
        """A client wired to this simulation."""
        clock = clock or SimClock()
        return GreenmailClient(GreenmailSettings(), http=self.http, connector=self.connect, clock=clock.now,
                               sleeper=clock.sleep)

# SPDX-FileCopyrightText: 2026 Aleksandr Polskiy
# SPDX-License-Identifier: Apache-2.0
"""A small in-process SMTP server for the real ``smtplib`` client (design §13.2, AMB-77).

It speaks just what the sender uses: greeting, ``EHLO`` / ``HELO``, ``MAIL``, ``RCPT``, ``DATA``,
``RSET``, ``NOOP`` and ``QUIT``, and keeps every message it accepts.
"""

import socketserver
import threading
from collections.abc import Iterator

from unittests.netfaults.sim_server import LoopbackServer, SimServer, lines_of

_REPLIES = {"EHLO": "250 sim", "HELO": "250 OK", "MAIL": "250 OK", "RCPT": "250 OK", "RSET": "250 OK",
            "NOOP": "250 OK", "QUIT": "221 bye"}


class _Handler(socketserver.StreamRequestHandler):
    """One SMTP session."""

    server: "_Server"

    def handle(self) -> None:
        """Greet, then answer commands until ``QUIT`` or the client goes away."""
        self._send("220 sim ESMTP ready")
        stream = lines_of(self.rfile)
        for line in stream:
            verb = line.decode("utf-8", errors="replace").strip().split(" ", 1)[0].upper()
            if verb == "DATA":
                self._send("354 end with <CRLF>.<CRLF>")
                self._receive(stream)
                self._send("250 OK queued")
                continue
            self._send(_REPLIES.get(verb, f"502 {verb} not implemented"))
            if verb == "QUIT":
                return

    def _receive(self, stream: Iterator[bytes]) -> None:
        """Read the message up to the lone dot and keep it; a message cut short is dropped."""
        body: list[bytes] = []
        for line in stream:
            if line == b".\r\n":
                with self.server.lock:
                    self.server.messages.append(b"".join(body))
                return
            body.append(line[1:] if line.startswith(b"..") else line)

    def _send(self, line: str) -> None:
        """Write one reply line; a client that went away is ignored."""
        try:
            self.wfile.write(line.encode("ascii") + b"\r\n")
        except OSError:
            pass


class _Server(LoopbackServer):
    """The listening server with the messages it accepted."""

    def __init__(self) -> None:
        """Bind a free loopback port."""
        super().__init__(_Handler)
        self.lock = threading.Lock()
        self.messages: list[bytes] = []


class SimSmtpServer(SimServer):
    """Start with ``with SimSmtpServer() as server:``; ``server.port``, ``server.messages``."""

    def __init__(self) -> None:
        """Create the server (not yet serving)."""
        self._smtp = _Server()
        super().__init__(self._smtp)

    @property
    def messages(self) -> list[bytes]:
        """Every message accepted, in order."""
        with self._smtp.lock:
            return list(self._smtp.messages)

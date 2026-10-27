# SPDX-FileCopyrightText: 2026 Aleksandr Polskiy
# SPDX-License-Identifier: Apache-2.0
"""What the simulated IMAP and SMTP servers share: a threaded loopback server and line reading."""

import socketserver
import threading
from collections.abc import Iterator
from types import TracebackType
from typing import BinaryIO, Self


def lines_of(stream: BinaryIO) -> Iterator[bytes]:
    """Yield a client's lines until it hangs up or the connection breaks.

    Args:
        stream: The handler's ``rfile``.

    Yields:
        Each line with its line end.
    """
    line = b""
    try:
        line = stream.readline()
        while line:
            yield line
            line = stream.readline()
    except OSError:
        line = b""  # the connection broke: the session simply ends


class LoopbackServer(socketserver.ThreadingTCPServer):
    """A threaded server on a free loopback port."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, handler: type[socketserver.BaseRequestHandler]) -> None:
        """Bind a free loopback port.

        Args:
            handler: The session handler.
        """
        super().__init__(("127.0.0.1", 0), handler)


class SimServer:
    """Serves a :class:`LoopbackServer` in a background thread inside a ``with`` block."""

    def __init__(self, server: LoopbackServer) -> None:
        """Wrap the server (not yet serving).

        Args:
            server: The bound server.
        """
        self._server = server
        self._thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)

    @property
    def port(self) -> int:
        """The listening port."""
        return int(self._server.server_address[1])

    def __enter__(self) -> Self:
        """Serve in a background thread."""
        self._thread.start()
        return self

    def __exit__(self, exc_type: type[BaseException] | None, exc: BaseException | None,
                 traceback: TracebackType | None) -> None:
        """Stop serving."""
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=2)

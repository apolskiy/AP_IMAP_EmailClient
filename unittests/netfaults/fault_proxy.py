# SPDX-FileCopyrightText: 2026 Aleksandr Polskiy
# SPDX-License-Identifier: Apache-2.0
"""A pure-Python TCP proxy that injects network faults between a real client and a server (design §13.2).

Modes, switchable at any time; a live connection follows the mode at its next chunk:

* ``pass``: forward both ways.
* ``latency``: forward after ``latency_sec`` per chunk.
* ``blackhole``: accept and never answer; the client's reads time out.
* ``reset_after_bytes``: forward ``after_bytes`` bytes to the client, then reset both sides.
* ``close_on_client_write``: let ``after_writes`` client writes through, then reset the connection on the
  next one (Windows 10053/10054, Linux ``BrokenPipe`` / ``ConnectionReset`` on the client's next call).
* ``refuse``: nothing listens on the port; connects are refused. Leaving it listens on the same port again.

With ``once=True`` a fault applies to one connection and the proxy then returns to ``pass``, so a test can
check that the client reconnects and recovers.
"""

import select
import socket
import struct
import threading
import time
from types import TracebackType

PASS = "pass"
LATENCY = "latency"
BLACKHOLE = "blackhole"
RESET_AFTER_BYTES = "reset_after_bytes"
CLOSE_ON_CLIENT_WRITE = "close_on_client_write"
REFUSE = "refuse"
MODES = (PASS, LATENCY, BLACKHOLE, RESET_AFTER_BYTES, CLOSE_ON_CLIENT_WRITE, REFUSE)
_CHUNK = 65536
_TICK_SEC = 0.05


def abort(sock: socket.socket) -> None:
    """Close with a TCP reset (``SO_LINGER`` 0) rather than an orderly close; never raises.

    Args:
        sock: The socket.
    """
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
    except OSError:
        pass
    try:
        sock.close()
    except OSError:
        pass


class _Link:
    """One client connection and, unless blackholed, its upstream connection."""

    def __init__(self, client: socket.socket, upstream: socket.socket | None) -> None:
        """Pair the two sockets."""
        self.client = client
        self.upstream = upstream
        self.to_client = 0
        self.client_writes = 0
        self.closed = threading.Event()

    def reset(self) -> None:
        """Reset both sides."""
        self.closed.set()
        abort(self.client)
        if self.upstream is not None:
            abort(self.upstream)

    def close(self) -> None:
        """Close both sides in order, as when one side hung up by itself."""
        self.closed.set()
        for sock in (self.client, self.upstream):
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass


class FaultProxy:  # pylint: disable=too-many-instance-attributes
    """Listens on a free loopback port and forwards to ``upstream_port``. Use as a context manager."""

    def __init__(self, upstream_port: int, upstream_host: str = "127.0.0.1") -> None:
        """Create a proxy in ``pass`` mode; :meth:`start` opens the port.

        Args:
            upstream_port: The server's port.
            upstream_host: The server's host.
        """
        self._upstream = (upstream_host, upstream_port)
        self._lock = threading.Lock()
        self._listener: socket.socket | None = None
        self._port = 0
        self._links: list[_Link] = []
        self._threads: list[threading.Thread] = []
        self._stopping = threading.Event()
        self.mode = PASS
        self.latency_sec = 0.0
        self.after_bytes = 0
        self.after_writes = 0
        self.once = False
        self.faults = 0

    @property
    def port(self) -> int:
        """The port clients connect to."""
        return self._port

    def start(self) -> "FaultProxy":
        """Listen on a free loopback port.

        Returns:
            The proxy.
        """
        self._listen(0)
        return self

    def set_mode(self, mode: str, *, latency_sec: float = 0.0, after_bytes: int = 0, after_writes: int = 0,
                 once: bool = False) -> None:
        """Switch the fault.

        Args:
            mode: One of ``MODES``.
            latency_sec: Delay per chunk (``latency``).
            after_bytes: Bytes forwarded to the client before the reset (``reset_after_bytes``).
            after_writes: Client writes let through before the reset (``close_on_client_write``).
            once: Apply the fault to one connection, then return to ``pass``.

        Raises:
            ValueError: For an unknown mode.
        """
        if mode not in MODES:
            raise ValueError(f"unknown fault mode {mode!r}; one of {MODES}")
        with self._lock:
            leaving_refuse = self.mode == REFUSE and mode != REFUSE
            self.mode, self.latency_sec, self.after_bytes = mode, latency_sec, after_bytes
            self.after_writes, self.once = after_writes, once
        if mode == REFUSE:
            self._close_listener()
        elif leaving_refuse:
            self._listen(self._port)

    def stop(self) -> None:
        """Stop listening and reset every connection."""
        self._stopping.set()
        self._close_listener()
        with self._lock:
            links = list(self._links)
        for link in links:
            link.reset()
        for thread in list(self._threads):
            thread.join(timeout=2)

    def __enter__(self) -> "FaultProxy":
        """Start listening."""
        return self.start()

    def __exit__(self, exc_type: type[BaseException] | None, exc: BaseException | None,
                 traceback: TracebackType | None) -> None:
        """Stop."""
        self.stop()

    # --- internals -------------------------------------------------------------------------------------

    def _spawn(self, target: object, *args: object) -> None:
        """Run a daemon thread and keep it for :meth:`stop`."""
        thread = threading.Thread(target=target, args=args, daemon=True)  # type: ignore[arg-type]
        self._threads.append(thread)
        thread.start()

    def _listen(self, port: int) -> None:
        """Open the listening socket (on ``port``, or a free one when 0)."""
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(("127.0.0.1", port))
        listener.listen(64)
        self._listener, self._port = listener, listener.getsockname()[1]
        self._spawn(self._accept, listener)

    def _close_listener(self) -> None:
        """Stop accepting; the port then refuses connections.

        On Linux a socket closed while another thread waits in ``accept`` keeps listening until that call
        returns, so the listener is shut down first (Windows refuses that call on a listening socket, which
        is harmless there) and the accept loop never blocks for more than one tick.
        """
        listener, self._listener = self._listener, None
        if listener is None:
            return
        try:
            listener.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        listener.close()

    def _fault_taken(self) -> None:
        """Count a fault; a one-time fault returns the proxy to ``pass``."""
        with self._lock:
            self.faults += 1
            if self.once:
                self.mode, self.once = PASS, False

    def _accept(self, listener: socket.socket) -> None:
        """Accept connections until the listener closes."""
        while not self._stopping.is_set() and self._listener is listener:
            if not self._readable(listener):
                continue
            try:
                client, _peer = listener.accept()
            except OSError:
                return
            if self.mode == BLACKHOLE:
                link = _Link(client, None)
                self._fault_taken()
                self._track(link)
                self._spawn(self._drain, link)
                continue
            try:
                upstream = socket.create_connection(self._upstream, timeout=5)
            except OSError:
                abort(client)
                continue
            link = _Link(client, upstream)
            self._track(link)
            self._spawn(self._pump, link, client, upstream, True)
            self._spawn(self._pump, link, upstream, client, False)

    def _track(self, link: _Link) -> None:
        """Remember a link so :meth:`stop` can reset it."""
        with self._lock:
            self._links.append(link)

    def _drain(self, link: _Link) -> None:
        """Blackhole: read and discard whatever the client sends, answer nothing."""
        while not link.closed.is_set() and not self._stopping.is_set():
            if not self._readable(link.client):
                continue
            try:
                if not link.client.recv(_CHUNK):
                    break
            except OSError:
                break
        link.close()

    @staticmethod
    def _readable(sock: socket.socket) -> bool:
        """Whether the socket has data (or EOF) within one tick; False on a closed socket."""
        try:
            ready, _writable, _failed = select.select([sock], [], [], _TICK_SEC)
        except (OSError, ValueError):
            return False
        return bool(ready)

    def _pump(self, link: _Link, source: socket.socket, target: socket.socket, from_client: bool) -> None:
        """Forward one direction, applying the current mode to every chunk."""
        while not link.closed.is_set() and not self._stopping.is_set():
            if not self._readable(source):
                continue
            try:
                chunk = source.recv(_CHUNK)
            except OSError:
                break
            if not chunk or not self._forward(link, target, chunk, from_client):
                break
        if not link.closed.is_set():
            link.close()

    def _forward(self, link: _Link, target: socket.socket, chunk: bytes, from_client: bool) -> bool:
        """Send one chunk under the current mode; False when the fault reset the link."""
        mode = self.mode
        if mode == LATENCY:
            time.sleep(self.latency_sec)
        if mode == CLOSE_ON_CLIENT_WRITE and from_client:
            link.client_writes += 1
            if link.client_writes > self.after_writes:
                self._fault_taken()
                link.reset()
                return False
        if mode == RESET_AFTER_BYTES and not from_client:
            room = self.after_bytes - link.to_client
            if len(chunk) >= room:
                self._send(target, chunk[:max(room, 0)])
                self._fault_taken()
                link.reset()
                return False
        if not from_client:
            link.to_client += len(chunk)
        return self._send(target, chunk)

    @staticmethod
    def _send(target: socket.socket, chunk: bytes) -> bool:
        """Send all of a chunk; False when the other side is gone."""
        try:
            target.sendall(chunk)
        except OSError:
            return False
        return True

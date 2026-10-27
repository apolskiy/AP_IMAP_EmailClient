# SPDX-FileCopyrightText: 2026 Aleksandr Polskiy
# SPDX-License-Identifier: Apache-2.0
"""IMAP transport: a small interface over stdlib ``imaplib`` (design §5.1).

Everything above this module talks to :class:`ImapTransport`, so unit tests can swap in a simulation that
fails on demand. Every low-level exception leaves this module as an :class:`ImapConnectionError`
carrying a keyword and a retryable flag (design §5.5).
"""

import imaplib
import re
import socket
import ssl
import threading
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, TypeVar

from emi_reader.errors import ImapConnectionError
from emi_reader.folders import decode_folder, encode_folder
from emi_reader.keywords import ReaderKeyword
from emi_reader.settings import ImapSettings, Security

_RESULT = TypeVar("_RESULT")
_LIMIT_MARKERS = ("too many", "maximum number of connections", "connection limit")
_STATUS_PATTERN = re.compile(r"(MESSAGES|UIDNEXT|UIDVALIDITY)\s+(\d+)")
_LIST_PATTERN = re.compile(r'^\((?P<flags>[^)]*)\)\s+(?:"[^"]*"|NIL)\s+(?P<name>.+)$')


class Phase(StrEnum):
    """Where an operation was when it failed; decides timeout and drop keywords."""

    CONNECT = "connect"
    LOGIN = "login"
    COMMAND = "command"


@dataclass(frozen=True)
class FolderInfo:
    """One ``LIST`` entry.

    Attributes:
        name: Folder name, decoded from modified UTF-7.
        flags: Lower-cased flags such as ``\\noselect`` or ``\\junk``.
    """

    name: str
    flags: frozenset[str]


@dataclass(frozen=True)
class FolderStatus:
    """Counters of one folder.

    Attributes:
        messages: Number of messages.
        uidnext: Next UID the server will assign.
        uidvalidity: The folder's UIDVALIDITY.
    """

    messages: int
    uidnext: int
    uidvalidity: int


class ImapTransport(Protocol):
    """Operations the reader needs from an IMAP connection."""

    def open(self) -> None:
        """Connect, secure the connection and log in."""

    def list_folders(self) -> list[FolderInfo]:
        """Return every folder the server lists."""

    def examine(self, folder: str) -> FolderStatus:
        """Select a folder read-only and return its counters."""

    def status(self, folder: str) -> FolderStatus:
        """Return a folder's counters without selecting it."""

    def uid_search(self, criteria: tuple[str, ...]) -> list[int]:
        """Search the selected folder; return matching UIDs."""

    def fetch_headers(self, uids: list[int]) -> dict[int, bytes]:
        """Fetch the header block of each UID, without setting flags."""

    def fetch_message(self, uid: int) -> bytes:
        """Fetch one full message, without setting flags."""

    def fetch_messages(self, uids: list[int]) -> dict[int, bytes]:
        """Fetch several full messages in one command, without setting flags."""

    def noop(self) -> None:
        """Check the connection is alive."""

    def close(self) -> None:
        """Log out and close; never raises."""


def _root_cause(error: BaseException) -> BaseException:
    """Return the original socket error that ``imaplib`` hides inside ``IMAP4.abort``."""
    cause = error.__cause__ or error.__context__
    return cause if isinstance(cause, OSError) else error


def classify(error: BaseException, phase: Phase, endpoint: str) -> ImapConnectionError:
    """Turn any low-level failure into a keyworded :class:`ImapConnectionError`.

    Args:
        error: What was raised.
        phase: Where it was raised.
        endpoint: ``host:port`` for diagnostics.

    Returns:
        The classified error; ``retryable`` is True only for faults a reconnect can heal.
    """
    root = _root_cause(error)
    text = str(error)
    keyword, retryable = _keyword_for(error, root, phase, text.lower())
    return ImapConnectionError(
        f"IMAP {phase} to {endpoint} failed: {type(root).__name__}: {root}",
        keyword,
        retryable=retryable,
        infrastructure=True,
        details={"endpoint": endpoint, "phase": str(phase), "error_type": type(root).__name__,
                 "errno": getattr(root, "errno", None)},
    )


# Faults no reconnect can heal (the server is down or misconfigured): fail at once.
_FATAL_TYPES: tuple[tuple[type[BaseException], ReaderKeyword], ...] = (
    (socket.gaierror, ReaderKeyword.IMAP_DNS_FAILURE),
    (ConnectionRefusedError, ReaderKeyword.IMAP_CONNECTION_REFUSED),
    (ssl.SSLError, ReaderKeyword.IMAP_TLS_HANDSHAKE_FAILED),
)


def _keyword_for(
    error: BaseException, root: BaseException, phase: Phase, lowered: str
) -> tuple[ReaderKeyword, bool]:
    """Pick the keyword and retryability for one failure (most specific check first)."""
    if any(marker in lowered for marker in _LIMIT_MARKERS):
        return ReaderKeyword.IMAP_CONNECTION_LIMIT, True
    for error_type, keyword in _FATAL_TYPES:
        if isinstance(root, error_type):
            return keyword, False
    if isinstance(root, TimeoutError):
        connecting = phase is Phase.CONNECT
        return (ReaderKeyword.IMAP_CONNECTION_TIMEOUT if connecting else ReaderKeyword.IMAP_READ_TIMEOUT,
                not connecting)
    is_abort = isinstance(error, imaplib.IMAP4.abort)
    if is_abort and root is not error:
        # imaplib wraps send() failures as abort("socket error: ...") with the OSError as context.
        return ReaderKeyword.IMAP_SOCKET_WRITE_FAILED, True
    if phase is Phase.LOGIN and isinstance(error, imaplib.IMAP4.error) and not is_abort:
        return ReaderKeyword.IMAP_AUTH_FAILED, False
    if is_abort or isinstance(root, (OSError, EOFError)):
        connecting = phase is Phase.CONNECT
        if connecting:
            return ReaderKeyword.IMAP_CONNECTION_REFUSED, False
        return ReaderKeyword.IMAP_CONNECTION_DROPPED, True
    return ReaderKeyword.IMAP_PROTOCOL_ERROR, False


def _parse_status(data: bytes | str) -> FolderStatus:
    """Parse ``MESSAGES``/``UIDNEXT``/``UIDVALIDITY`` counters from a STATUS or SELECT reply."""
    text = data.decode("ascii", errors="replace") if isinstance(data, bytes) else data
    values = {name.upper(): int(number) for name, number in _STATUS_PATTERN.findall(text)}
    return FolderStatus(
        messages=values.get("MESSAGES", 0),
        uidnext=values.get("UIDNEXT", 0),
        uidvalidity=values.get("UIDVALIDITY", 0),
    )


def quote_folder(folder: str) -> str:
    """Encode (modified UTF-7) and quote a folder name for an IMAP command.

    Args:
        folder: Unicode folder name.

    Returns:
        The quoted wire form.
    """
    escaped = encode_folder(folder).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


class ImaplibTransport:
    """:class:`ImapTransport` over ``imaplib``; one instance is one connection.

    Not thread-safe by itself (``imaplib`` is not), so every command runs under one lock.
    """

    def __init__(self, settings: ImapSettings) -> None:
        """Create an unopened transport.

        Args:
            settings: Connection settings.
        """
        self._settings = settings
        self._client: imaplib.IMAP4 | None = None
        self._lock = threading.Lock()

    @property
    def settings(self) -> ImapSettings:
        """The connection settings."""
        return self._settings

    def _ssl_context(self) -> ssl.SSLContext:
        """Build the TLS context from the settings."""
        context = ssl.create_default_context(cafile=self._settings.ca_file)
        if not self._settings.verify_tls:
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
        return context

    def _run(self, phase: Phase, action: Callable[[], _RESULT]) -> _RESULT:
        """Run one operation under the lock and classify any failure."""
        with self._lock:
            try:
                return action()
            except (OSError, EOFError, imaplib.IMAP4.error) as error:
                raise classify(error, phase, self._settings.endpoint) from error

    def _require_client(self) -> imaplib.IMAP4:
        """Return the open client or fail as a dropped connection."""
        if self._client is None:
            raise ImapConnectionError(
                "IMAP connection is not open",
                ReaderKeyword.IMAP_CONNECTION_DROPPED,
                retryable=True,
                infrastructure=True,
                details={"endpoint": self._settings.endpoint},
            )
        return self._client

    def _checked(self, response: tuple[str, list[bytes | None]], command: str) -> list[bytes | None]:
        """Return the data of an ``OK`` reply; raise a protocol error otherwise."""
        result, data = response
        if result != "OK":
            raise ImapConnectionError(
                f"IMAP {command} returned {result}: {data!r}",
                ReaderKeyword.IMAP_PROTOCOL_ERROR,
                infrastructure=True,
                details={"endpoint": self._settings.endpoint, "command": command},
            )
        return data

    def open(self) -> None:
        """Connect, secure the connection and log in; does nothing if already open."""
        if self._client is not None:
            return
        settings = self._settings

        def _connect() -> imaplib.IMAP4:
            if settings.security is Security.SSL:
                return imaplib.IMAP4_SSL(
                    settings.host, settings.effective_port, ssl_context=self._ssl_context(),
                    timeout=settings.connect_timeout,
                )
            client = imaplib.IMAP4(settings.host, settings.effective_port, timeout=settings.connect_timeout)
            if settings.security is Security.STARTTLS:
                client.starttls(ssl_context=self._ssl_context())
            return client

        client = self._run(Phase.CONNECT, _connect)
        client.sock.settimeout(settings.read_timeout)
        self._client = client
        self._run(Phase.LOGIN, lambda: client.login(settings.username, settings.password))

    def list_folders(self) -> list[FolderInfo]:
        """Return every folder the server lists."""
        client = self._require_client()
        data = self._checked(self._run(Phase.COMMAND, client.list), "LIST")
        folders: list[FolderInfo] = []
        for entry in data:
            if not entry:
                continue
            match = _LIST_PATTERN.match(entry.decode("utf-8", errors="replace"))
            if match:
                flags = frozenset(flag.lower() for flag in match.group("flags").split())
                wire_name = match.group("name").strip().strip('"')
                folders.append(FolderInfo(name=decode_folder(wire_name), flags=flags))
        return folders

    def examine(self, folder: str) -> FolderStatus:
        """Select a folder read-only (``EXAMINE``) and return its counters."""
        client = self._require_client()
        self._checked(self._run(Phase.COMMAND, lambda: client.select(quote_folder(folder), readonly=True)),
                      "EXAMINE")
        return self.status(folder)

    def status(self, folder: str) -> FolderStatus:
        """Return a folder's counters without selecting it."""
        client = self._require_client()
        data = self._checked(
            self._run(Phase.COMMAND,
                      lambda: client.status(quote_folder(folder), "(MESSAGES UIDNEXT UIDVALIDITY)")),
            "STATUS",
        )
        return _parse_status(data[0] or b"")

    def uid_search(self, criteria: tuple[str, ...]) -> list[int]:
        """Search the selected folder; return matching UIDs in ascending order."""
        client = self._require_client()
        data = self._checked(self._run(Phase.COMMAND, lambda: client.uid("SEARCH", *criteria)), "SEARCH")
        raw = data[0] or b""
        return sorted(int(token) for token in raw.split())

    def fetch_headers(self, uids: list[int]) -> dict[int, bytes]:
        """Fetch header blocks with ``BODY.PEEK[HEADER]`` (flags untouched)."""
        return self._fetch(uids, "BODY.PEEK[HEADER]")

    def fetch_message(self, uid: int) -> bytes:
        """Fetch one full message with ``BODY.PEEK[]`` (flags untouched)."""
        fetched = self._fetch([uid], "BODY.PEEK[]")
        if uid not in fetched:
            raise ImapConnectionError(
                f"message UID {uid} not returned by FETCH",
                ReaderKeyword.IMAP_PROTOCOL_ERROR,
                infrastructure=True,
                details={"endpoint": self._settings.endpoint, "uid": uid},
            )
        return fetched[uid]

    def fetch_messages(self, uids: list[int]) -> dict[int, bytes]:
        """Fetch several full messages with one ``UID FETCH ... BODY.PEEK[]`` (flags untouched).

        One command, because a server such as Greenmail spends time per command that grows with the
        folder, whatever the number of messages (design §5.4, AMB-71). A UID missing from the reply
        (expunged meanwhile) is simply absent from the result.
        """
        return self._fetch(uids, "BODY.PEEK[]")

    def _fetch(self, uids: list[int], item: str) -> dict[int, bytes]:
        """Run ``UID FETCH`` and map each UID to its literal."""
        if not uids:
            return {}
        client = self._require_client()
        uid_set = ",".join(str(uid) for uid in uids)
        data = self._checked(self._run(Phase.COMMAND, lambda: client.uid("FETCH", uid_set, f"(UID {item})")),
                             "FETCH")
        result: dict[int, bytes] = {}
        for entry in data:
            if isinstance(entry, tuple) and len(entry) == 2:
                uid_match = re.search(rb"UID (\d+)", entry[0])
                if uid_match:
                    result[int(uid_match.group(1))] = entry[1]
        return result

    def noop(self) -> None:
        """Check the connection is alive."""
        client = self._require_client()
        self._checked(self._run(Phase.COMMAND, client.noop), "NOOP")

    def close(self) -> None:
        """Log out and close; never raises."""
        client, self._client = self._client, None
        if client is None:
            return
        try:
            client.logout()
        except (OSError, EOFError, imaplib.IMAP4.error):
            pass

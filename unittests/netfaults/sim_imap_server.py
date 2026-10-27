# SPDX-FileCopyrightText: 2026 Aleksandr Polskiy
# SPDX-License-Identifier: Apache-2.0
"""A small in-process IMAP server for the real ``imaplib`` client (design §13.2, AMB-77).

It speaks just what the reader uses: greeting, ``CAPABILITY``, ``LOGIN``, ``LIST``, ``EXAMINE`` /
``SELECT``, ``STATUS``, ``UID SEARCH`` (``UID n:*`` plus one recipient header), ``UID FETCH``
(``BODY.PEEK[]`` and ``BODY.PEEK[HEADER]``), ``NOOP`` and ``LOGOUT``. One folder, ``INBOX``.
"""

import re
import socketserver
import threading
from email import message_from_bytes
from email.utils import getaddresses

from unittests.netfaults.sim_server import LoopbackServer, SimServer, lines_of

_TOKEN = re.compile(r'"(?:[^"\\]|\\.)*"|\(.*?\)|\S+')
_UID_VALIDITY = 1
_SEARCH_HEADERS = {"TO": "To", "CC": "Cc", "BCC": "Bcc"}


def _unquote(token: str) -> str:
    """A quoted IMAP string's value."""
    return token[1:-1].replace('\\"', '"').replace("\\\\", "\\") if token.startswith('"') else token


class _Store:
    """The messages of ``INBOX``, UIDs from 1."""

    def __init__(self) -> None:
        """Empty."""
        self.lock = threading.Lock()
        self.messages: dict[int, bytes] = {}
        self.next_uid = 1

    def add(self, raw: bytes) -> int:
        """Store one message; return its UID."""
        with self.lock:
            uid = self.next_uid
            self.messages[uid] = raw
            self.next_uid += 1
            return uid

    def search(self, criteria: list[str]) -> list[int]:
        """``[UID n:*] <TO|CC|BCC|HEADER name> "address"``; like a real server, ``n:*`` keeps the top UID."""
        with self.lock:
            uids = sorted(self.messages)
            if criteria and criteria[0].upper() == "UID":
                first = int(criteria[1].partition(":")[0])
                criteria = criteria[2:]
                newer = [uid for uid in uids if uid >= first]
                uids = newer if newer else uids[-1:]
            if not criteria:
                return uids
            key = criteria[0].upper()
            header = _unquote(criteria[1]) if key == "HEADER" else _SEARCH_HEADERS.get(key, key)
            address = _unquote(criteria[-1]).lower()
            return [uid for uid in uids if address in self._addresses(uid, header)]

    def _addresses(self, uid: int, header: str) -> set[str]:
        """The addresses in one header of a message."""
        values = message_from_bytes(self.messages[uid]).get_all(header) or []
        return {address.lower() for _name, address in getaddresses([str(value) for value in values])}


class _Handler(socketserver.StreamRequestHandler):
    """One IMAP session."""

    server: "_Server"

    def handle(self) -> None:
        """Greet, then answer commands until ``LOGOUT`` or the client goes away."""
        self._send("* OK [CAPABILITY IMAP4rev1] sim ready")
        for line in lines_of(self.rfile):
            tag, _space, rest = line.decode("utf-8", errors="replace").rstrip("\r\n").partition(" ")
            tokens = _TOKEN.findall(rest)
            if not tokens:
                self._send(f"{tag} BAD empty command")
            elif not self._dispatch(tag, tokens[0].upper(), tokens[1:]):
                return

    def _dispatch(self, tag: str, command: str, args: list[str]) -> bool:
        """Answer one command; False after ``LOGOUT``."""
        store = self.server.store
        if command == "CAPABILITY":
            self._send("* CAPABILITY IMAP4rev1")
        elif command == "LOGIN":
            if _unquote(args[1]) != self.server.password:
                self._send(f"{tag} NO [AUTHENTICATIONFAILED] invalid credentials")
                return True
        elif command == "LIST":
            self._send('* LIST (\\HasNoChildren) "/" INBOX')
        elif command in ("EXAMINE", "SELECT"):
            with store.lock:
                count, next_uid = len(store.messages), store.next_uid
            self._send(f"* {count} EXISTS")
            self._send(f"* OK [UIDVALIDITY {_UID_VALIDITY}] UIDs valid")
            self._send(f"* OK [UIDNEXT {next_uid}] next UID")
            self._send(f"{tag} OK [READ-ONLY] {command} completed")
            return True
        elif command == "STATUS":
            with store.lock:
                count, next_uid = len(store.messages), store.next_uid
            self._send(f"* STATUS INBOX (MESSAGES {count} UIDNEXT {next_uid} UIDVALIDITY {_UID_VALIDITY})")
        elif command == "UID" and args and args[0].upper() == "SEARCH":
            self._send("* SEARCH " + " ".join(str(uid) for uid in store.search(args[1:])))
        elif command == "UID" and args and args[0].upper() == "FETCH":
            self._fetch(args[1], " ".join(args[2:]).upper())
        elif command == "LOGOUT":
            self._send("* BYE sim closing")
            self._send(f"{tag} OK LOGOUT completed")
            return False
        elif command != "NOOP":
            self._send(f"{tag} BAD unknown command {command}")
            return True
        self._send(f"{tag} OK {command} completed")
        return True

    def _fetch(self, uid_set: str, items: str) -> None:
        """Answer ``UID FETCH`` with each message's literal."""
        store = self.server.store
        with store.lock:
            wanted = [int(part) for part in uid_set.split(",") if part.isdigit()]
            found = [(uid, store.messages[uid]) for uid in wanted if uid in store.messages]
            sequence = {uid: index for index, uid in enumerate(sorted(store.messages), 1)}
        header_only = "HEADER" in items
        for uid, raw in found:
            body = raw.split(b"\r\n\r\n", 1)[0] + b"\r\n\r\n" if header_only else raw
            section = "BODY[HEADER]" if header_only else "BODY[]"
            self._write(f"* {sequence[uid]} FETCH (UID {uid} {section} {{{len(body)}}}\r\n".encode("ascii")
                        + body + b")\r\n")

    def _send(self, line: str) -> None:
        """Write one line."""
        self._write(line.encode("utf-8") + b"\r\n")

    def _write(self, data: bytes) -> None:
        """Write bytes; a client that went away is ignored."""
        try:
            self.wfile.write(data)
        except OSError:
            pass


class _Server(LoopbackServer):
    """The listening server with its store."""

    def __init__(self, password: str) -> None:
        """Bind a free loopback port."""
        super().__init__(_Handler)
        self.store = _Store()
        self.password = password


class SimImapServer(SimServer):
    """Start with ``with SimImapServer() as server:``; ``server.port``, ``server.deliver(raw)``."""

    def __init__(self, password: str = "password") -> None:
        """Create the server (not yet serving).

        Args:
            password: The password ``LOGIN`` accepts (any user name).
        """
        self._imap = _Server(password)
        super().__init__(self._imap)

    def deliver(self, raw: bytes) -> int:
        """Put one message in ``INBOX``.

        Args:
            raw: The message bytes.

        Returns:
            Its UID.
        """
        return self._imap.store.add(raw)

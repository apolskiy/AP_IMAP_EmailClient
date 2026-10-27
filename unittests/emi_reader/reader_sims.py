# SPDX-FileCopyrightText: 2026 Aleksandr Polskiy
# SPDX-License-Identifier: Apache-2.0
"""Test doubles for the reader: a simulated clock, a simulated IMAP transport and email builders."""

import base64
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from email.message import EmailMessage
from pathlib import Path

from emi_reader.errors import ImapConnectionError
from emi_reader.keywords import ReaderKeyword
from emi_reader.mime import parse_email
from emi_reader.settings import ImapSettings
from emi_reader.transport import FolderInfo, FolderStatus
from unittests.sims import SimClock

FIXTURES = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "eml"


def make_jwt(payload: dict[str, object]) -> str:
    """Build a JWT-shaped token with a dummy signature."""
    def _segment(value: dict[str, object]) -> str:
        return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")
    return f"{_segment({'alg': 'HS256', 'typ': 'JWT'})}.{_segment(payload)}.c2lnbmF0dXJl"


def make_email(  # pylint: disable=too-many-arguments
    *,
    to_addr: str = "",
    cc_addr: str = "",
    label: str = "Activate account",
    path: str = "/activate",
    html: bool = True,
    text: bool = True,
    text_path: str | None = None,
    test_id: str = "",
    run_id: str = "",
) -> bytes:
    """Build a small activation-style email.

    Args:
        to_addr: ``To`` header.
        cc_addr: ``Cc`` header.
        label: Button text in the HTML part.
        path: Link path in the HTML part.
        html: Include an HTML part.
        text: Include a plain-text part.
        text_path: Link path in the text part (defaults to ``path``).
        test_id: ``X-EMI-Test-Id`` marker.
        run_id: ``X-EMI-Run-Id`` marker.

    Returns:
        The message bytes.
    """
    recipient = to_addr or cc_addr
    token = make_jwt({"sub": recipient})
    link = f"https://cloud.example.com{path}?code={token}&lang=en"
    text_link = f"https://cloud.example.com{text_path or path}?code={token}&lang=en"
    message = EmailMessage()
    message["Subject"] = "Activate your account"
    message["From"] = "noreply@example.com"
    if to_addr:
        message["To"] = to_addr
    if cc_addr:
        message["Cc"] = cc_addr
    message["Message-ID"] = f"<{abs(hash((recipient, label, path, test_id, run_id)))}@example.com>"
    if test_id:
        message["X-EMI-Test-Id"] = test_id
    if run_id:
        message["X-EMI-Run-Id"] = run_id
    if text:
        message.set_content(f"Open the link below:\n{text_link}\n\nWebsite: https://www.example.com\n")
    if html:
        body = (f'<p><a href="https://cloud.example.com/?utm=1"><img alt=""></a></p>'
                f'<p><a class="btn" href="{link}">{label}</a></p>'
                f'<p><a href="https://www.example.com">www.example.com</a></p>')
        if text:
            message.add_alternative(body, subtype="html")
        else:
            message.set_content(body, subtype="html")
    return message.as_bytes()


def _matches(tokens: list[str], addresses_in: Callable[[tuple[str, ...]], tuple[str, ...]]) -> bool:
    """Evaluate one search key (consumed from ``tokens``): ``OR a b``, ``HEADER name "x"`` or ``TO "x"``."""
    head = tokens.pop(0)
    if head == "OR":
        first = _matches(tokens, addresses_in)
        second = _matches(tokens, addresses_in)
        return first or second
    field_name = tokens.pop(0) if head == "HEADER" else head
    return tokens.pop(0).strip('"').lower() in addresses_in((field_name.lower(),))


@dataclass
class SimFolder:
    """One folder of the simulated server."""

    messages: dict[int, bytes] = field(default_factory=dict)
    uidvalidity: int = 1
    uidnext: int = 1
    flags: frozenset[str] = frozenset()
    selectable: bool = True
    status_allowed: bool = True

    def add(self, raw: bytes) -> int:
        """Store a message and return its UID."""
        uid = self.uidnext
        self.messages[uid] = raw
        self.uidnext += 1
        return uid


class SimImap:
    """In-memory IMAP server implementing the reader's transport protocol.

    Search matches exactly, like Greenmail. ``failures`` maps a method name to a list of errors raised
    on its next calls; ``arrivals`` delivers messages once the simulated clock reaches their time.
    """

    def __init__(self, clock: SimClock | None = None) -> None:
        """Create a server with an empty INBOX."""
        self.folders: dict[str, SimFolder] = {"INBOX": SimFolder()}
        self.failures: dict[str, list[Exception]] = {}
        self.arrivals: list[tuple[float, str, bytes]] = []
        self.calls: list[str] = []
        self.searches: list[tuple[str, ...]] = []
        self.opens = 0
        self.unlisted: set[str] = set()
        self._clock = clock
        self._selected: str | None = None
        self._open = False

    def __call__(self, _settings: ImapSettings) -> "SimImap":
        """Act as the transport factory: every connection is this same server object."""
        return self

    def deliver(self, folder: str, raw: bytes) -> int:
        """Put a message into a folder now."""
        return self.folders.setdefault(folder, SimFolder()).add(raw)

    def _enter(self, name: str) -> None:
        """Record a call, deliver due arrivals and raise a scripted failure."""
        self.calls.append(name)
        if self._clock is not None:
            due = [item for item in self.arrivals if item[0] <= self._clock.now()]
            for item in due:
                self.arrivals.remove(item)
                self.deliver(item[1], item[2])
        pending = self.failures.get(name)
        if pending:
            raise pending.pop(0)
        if name != "open" and not self._open:
            raise ImapConnectionError("not open", ReaderKeyword.IMAP_CONNECTION_DROPPED, retryable=True)

    def open(self) -> None:
        """Log in (counts real reconnects)."""
        if self._open:
            return
        self._enter("open")
        self.opens += 1
        self._open = True

    def list_folders(self) -> list[FolderInfo]:
        """List folders except the hidden ones."""
        self._enter("list_folders")
        return [FolderInfo(name, folder.flags) for name, folder in self.folders.items()
                if name not in self.unlisted]

    def examine(self, folder: str) -> FolderStatus:
        """Select read-only; a non-selectable folder answers NO."""
        self._enter("examine")
        entry = self.folders[folder]
        if not entry.selectable:
            raise ImapConnectionError("NO access denied", ReaderKeyword.IMAP_PROTOCOL_ERROR,
                                      infrastructure=True)
        self._selected = folder
        return FolderStatus(len(entry.messages), entry.uidnext, entry.uidvalidity)

    def status(self, folder: str) -> FolderStatus:
        """Counters without selecting."""
        self._enter("status")
        entry = self.folders[folder]
        if not entry.status_allowed:
            raise ImapConnectionError("NO", ReaderKeyword.IMAP_PROTOCOL_ERROR, infrastructure=True)
        return FolderStatus(len(entry.messages), entry.uidnext, entry.uidvalidity)

    def uid_search(self, criteria: tuple[str, ...]) -> list[int]:
        """Support ``UID n:*``, exact recipient searches, ``OR`` of them (the shared poller's search) and a
        range combined with either (``UID n:* OR TO "a" CC "b"``).

        Like a real server, ``n:*`` with ``n`` above every UID still matches the highest UID (RFC 3501).
        """
        self._enter("uid_search")
        self.searches.append(criteria)
        messages = self.folders[self._selected or "INBOX"].messages
        uids = sorted(messages)
        if criteria[0] == "UID":
            start = int(criteria[1].split(":")[0])
            uids = [uid for uid in uids if uid >= start] or uids[-1:]
            criteria = criteria[2:]
        if not criteria:
            return uids
        return [uid for uid in uids if _matches(list(criteria), parse_email(messages[uid]).addresses_in)]

    def fetch_headers(self, uids: list[int]) -> dict[int, bytes]:
        """Return header blocks."""
        self._enter("fetch_headers")
        messages = self.folders[self._selected or "INBOX"].messages
        return {uid: messages[uid].split(b"\n\n", 1)[0] + b"\n\n" for uid in uids if uid in messages}

    def fetch_message(self, uid: int) -> bytes:
        """Return a full message."""
        self._enter("fetch_message")
        return self.folders[self._selected or "INBOX"].messages[uid]

    def fetch_messages(self, uids: list[int]) -> dict[int, bytes]:
        """Return several full messages in one call (a UID no longer present is left out)."""
        self._enter("fetch_messages")
        messages = self.folders[self._selected or "INBOX"].messages
        return {uid: messages[uid] for uid in uids if uid in messages}

    def noop(self) -> None:
        """Do nothing."""
        self._enter("noop")

    def close(self) -> None:
        """Disconnect."""
        self._open = False

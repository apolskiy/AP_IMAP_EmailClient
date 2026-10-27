# SPDX-FileCopyrightText: 2026 Aleksandr Polskiy
# SPDX-License-Identifier: Apache-2.0
"""Test doubles for the broker: a simulated mailbox, sender and purge connection."""

import socket
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from emi_reader import Cta, ImapSettings, ParsedEmail, RawEmail, ReceivedEmail
from emi_sender import BatchRequest, Manifest, ManifestEntry, Marker
from emi_test_broker import BrokerService, BrokerSettings
from emi_test_broker.server import bind, serve
from unittests.sims import SimClock

LINK = "https://cloud.example.com/activate?code=abc"


def free_port() -> int:
    """A loopback port nothing listens on right now."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def received(address: str, uid: int = 1) -> ReceivedEmail:
    """One received activation email."""
    parsed = ParsedEmail(message_id=f"<m{uid}@example.com>", subject="Activate", recipients={"to": (address,)},
                         html=f'<a href="{LINK}">Activate account</a>', text=LINK, test_id="400001", run_id="run-1")
    return ReceivedEmail(uid=uid, folder="INBOX", email=parsed, link=LINK)


class SimMailbox:
    """Mailbox that answers from a script; ``on_wait`` runs inside each wait."""

    def __init__(self, settings: ImapSettings) -> None:
        """Start with a successful script."""
        self.settings = settings
        self.error: Exception | None = None
        self.on_wait: Callable[[], None] | None = None
        self.waits: list[dict[str, Any]] = []
        self.closed = 0
        self.inventory: dict[str, Any] | None = {"address": "qa@example.com", "match_fields": ["to"],
                                                 "before": {"total_messages": 3}, "after": {"total_messages": 4}}

    @property
    def access_matrix(self) -> list[dict[str, Any]]:
        """One accessible folder."""
        return [{"account": "qa", "folder": "INBOX", "access": "accessible"}]

    @property
    def attempts(self) -> list[dict[str, Any]]:
        """No retried faults."""
        return []

    def _wait(self, **request: Any) -> None:
        """Record and run the script."""
        self.waits.append(request)
        if self.on_wait is not None:
            self.on_wait()
        if self.error is not None:
            raise self.error

    def wait_for_link(self, address: str, cta: Cta, **options: Any) -> str:
        """Return the link."""
        self._wait(address=address, cta=cta, **options)
        return LINK

    def wait_for_emails(self, address: str, **options: Any) -> list[ReceivedEmail]:
        """Return ``count`` emails."""
        self._wait(address=address, **options)
        return [received(address, uid) for uid in range(1, int(options.get("count", 1)) + 1)]

    def emails_to(self, addresses: tuple[str, ...], limit: int = 30) -> list[RawEmail]:
        """One stored email per address (bytes that are not ASCII, to prove they cross unchanged)."""
        self._wait(addresses=addresses, limit=limit)
        return [RawEmail(uid=index, folder="INBOX", address=address, subject="Activate",
                         raw=f"To: {address}\r\nSubject: Activate\r\n\r\nPrüfung\r\n".encode("latin-1"))
                for index, address in enumerate(addresses[:limit], 1)]

    def close(self) -> None:
        """Count closes."""
        self.closed += 1


class SimMailboxes:
    """Factory of simulated mailboxes that keeps every one it built."""

    def __init__(self) -> None:
        """No mailbox yet."""
        self.built: list[SimMailbox] = []
        self.error: Exception | None = None
        self.on_wait: Callable[[], None] | None = None

    def __call__(self, settings: ImapSettings) -> SimMailbox:
        """Build one mailbox with the current script."""
        mailbox = SimMailbox(settings)
        mailbox.error = self.error
        mailbox.on_wait = self.on_wait
        self.built.append(mailbox)
        return mailbox


class SimSender:
    """Sender that records batches and marks every email sent."""

    def __init__(self) -> None:
        """Nothing sent yet."""
        self.batches: list[tuple[BatchRequest, Marker | None]] = []

    def send_batch(self, request: BatchRequest, *, marker: Marker | None = None) -> Manifest:
        """Return a manifest with one sent entry per address and kind."""
        self.batches.append((request, marker))
        entries = [ManifestEntry(index=index, address=address, field="to", kind=kind, status="sent", link=LINK)
                   for index, (address, kind) in enumerate((address, kind) for address in request.addresses
                                                           for kind in request.counts)]
        marker = marker or Marker()
        return Manifest(entries=entries, seed=7, test_id=marker.test_id, run_id=marker.run_id)


class SimPurge:
    """Read-write IMAP connection over a dictionary of folder -> UIDs."""

    def __init__(self, folders: dict[str, list[int]]) -> None:
        """Hold the simulated folders."""
        self.folders = folders
        self.selected = ""
        self.criteria: list[str] = []
        self.deleted: list[int] = []
        self.expunged = 0
        self.closed = False

    def __call__(self, settings: ImapSettings) -> "SimPurge":
        """Act as the factory too."""
        del settings
        return self

    def select(self, folder: str) -> None:
        """Open a folder."""
        self.selected = folder

    def uid_search(self, criteria: str) -> list[int]:
        """Every UID of the folder."""
        self.criteria.append(criteria)
        return list(self.folders.get(self.selected, []))

    def mark_deleted(self, uids: list[int]) -> None:
        """Record deletions."""
        self.deleted.extend(uids)

    def expunge(self) -> None:
        """Count expunges."""
        self.expunged += 1

    def close(self) -> None:
        """Record logout."""
        self.closed = True


def start_served(tmp_path: Path) -> tuple[BrokerSettings, BrokerService, SimMailboxes, threading.Thread]:
    """A real broker on a free loopback port, wired to simulations; the caller stops it and joins."""
    settings = BrokerSettings(port=free_port(), log_dir=tmp_path / "logs", state_dir=tmp_path / "state")
    mailboxes = SimMailboxes()
    service = BrokerService(settings, mailbox_factory=mailboxes, sender=SimSender(),  # type: ignore[arg-type]
                            purge_factory=SimPurge({"INBOX": []}))  # type: ignore[arg-type]
    server, state = bind(service)
    thread = threading.Thread(target=serve, args=(server, state), daemon=True)
    thread.start()
    return settings, service, mailboxes, thread


def make_service(tmp_path: Path, clock: SimClock | None = None, **settings: Any) -> tuple[
        BrokerService, SimMailboxes, SimSender, SimPurge]:
    """A service wired to simulations, logging under ``tmp_path``."""
    mailboxes, sender, purge = SimMailboxes(), SimSender(), SimPurge({"INBOX": [3, 4]})
    clock = clock or SimClock()
    values: dict[str, Any] = {"log_dir": tmp_path / "logs", "state_dir": tmp_path / "state"}
    values.update(settings)
    service = BrokerService(BrokerSettings(**values), mailbox_factory=mailboxes,  # type: ignore[arg-type]
                            sender=sender, purge_factory=purge, clock=clock.now)  # type: ignore[arg-type]
    return service, mailboxes, sender, purge

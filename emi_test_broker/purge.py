# SPDX-FileCopyrightText: 2026 Aleksandr Polskiy
# SPDX-License-Identifier: Apache-2.0
"""Manual mailbox purge: ``STORE +FLAGS \\Deleted`` + ``EXPUNGE`` (design §7).

Nothing in the test flow calls this. The broker refuses it while any run is active. The default scope is
only mail carrying the EMI marker; ``all`` widens it to every email in the folders.
"""

import imaplib
import ssl
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Protocol

from emi_reader import ImapSettings, Security
from emi_reader.transport import quote_folder

_MARKER_HEADER = "X-EMI-Test-Id"
_IMAP_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


class PurgeTransport(Protocol):
    """The few write commands a purge needs."""

    def select(self, folder: str) -> None:
        """Open a folder read-write."""

    def uid_search(self, criteria: str) -> list[int]:
        """Return the UIDs matching an IMAP search."""

    def mark_deleted(self, uids: list[int]) -> None:
        """Flag messages ``\\Deleted``."""

    def expunge(self) -> None:
        """Remove flagged messages."""

    def close(self) -> None:
        """Log out."""


@dataclass(frozen=True)
class PurgeRequest:
    """What to purge.

    Attributes:
        dry_run: Count only; delete nothing.
        older_than_days: Only mail received before this many days ago (IMAP ``BEFORE`` is day-granular).
        everything: Every email, not only mail with the EMI marker.
        requested_by: Who ran it, for the log.
    """

    dry_run: bool = False
    older_than_days: int | None = None
    everything: bool = False
    requested_by: str = ""


@dataclass
class PurgeReport:
    """Counts per folder and category."""

    dry_run: bool
    scope: str
    counts: dict[str, int] = field(default_factory=dict)

    @property
    def total(self) -> int:
        """Emails deleted (or that would be, on a dry run)."""
        return sum(self.counts.values())


def search_criteria(request: PurgeRequest, today: date) -> str:
    """IMAP search criteria for a purge request.

    Args:
        request: The request.
        today: Today's date.

    Returns:
        For example ``HEADER X-EMI-Test-Id "" BEFORE 19-Oct-2026``.
    """
    parts = ["ALL"] if request.everything else [f'HEADER {_MARKER_HEADER} ""']
    if request.older_than_days is not None:
        cutoff = today - timedelta(days=request.older_than_days)
        parts.append(f"BEFORE {cutoff.day:02d}-{_IMAP_MONTHS[cutoff.month - 1]}-{cutoff.year}")
    return " ".join(parts)


def run_purge(transport: PurgeTransport, folders: tuple[str, ...], request: PurgeRequest, today: date) -> PurgeReport:
    """Purge each folder.

    Args:
        transport: An opened write transport.
        folders: Folders to purge.
        request: Scope and dry-run flag.
        today: Today's date.

    Returns:
        Counts per folder.
    """
    criteria = search_criteria(request, today)
    report = PurgeReport(dry_run=request.dry_run, scope=criteria)
    try:
        for folder in folders:
            transport.select(folder)
            uids = transport.uid_search(criteria)
            report.counts[folder] = len(uids)
            if uids and not request.dry_run:
                transport.mark_deleted(uids)
                transport.expunge()
    finally:
        transport.close()
    return report


class ImaplibPurgeTransport:
    """``imaplib`` implementation of :class:`PurgeTransport` (read-write, unlike the reader)."""

    def __init__(self, settings: ImapSettings) -> None:
        """Connect and log in.

        Args:
            settings: Connection settings.
        """
        context = ssl.create_default_context(cafile=settings.ca_file)
        if not settings.verify_tls:
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
        if settings.security is Security.SSL:
            self._imap: imaplib.IMAP4 = imaplib.IMAP4_SSL(settings.host, settings.effective_port,
                                                          ssl_context=context, timeout=settings.connect_timeout)
        else:
            self._imap = imaplib.IMAP4(settings.host, settings.effective_port, timeout=settings.connect_timeout)
            if settings.security is Security.STARTTLS:
                self._imap.starttls(context)
        self._imap.login(settings.username, settings.password)

    def select(self, folder: str) -> None:
        """Open a folder read-write.

        Args:
            folder: Folder name.
        """
        self._check(self._imap.select(quote_folder(folder)))

    def uid_search(self, criteria: str) -> list[int]:
        """Search by UID.

        Args:
            criteria: IMAP search criteria.

        Returns:
            Matching UIDs.
        """
        data = self._check(self._imap.uid("SEARCH", criteria))
        return [int(item) for item in (data[0] or b"").split()]

    def mark_deleted(self, uids: list[int]) -> None:
        """Flag messages ``\\Deleted``.

        Args:
            uids: Message UIDs.
        """
        self._check(self._imap.uid("STORE", ",".join(str(uid) for uid in uids), "+FLAGS", "(\\Deleted)"))

    def expunge(self) -> None:
        """Remove flagged messages."""
        self._check(self._imap.expunge())

    def close(self) -> None:
        """Log out; errors are ignored."""
        try:
            self._imap.logout()
        except (imaplib.IMAP4.error, OSError):
            pass

    @staticmethod
    def _check(response: tuple[str, list[bytes | None]]) -> list[bytes | None]:
        """Raise on a non-OK answer."""
        status, data = response
        if status != "OK":
            raise imaplib.IMAP4.error(f"IMAP command failed: {status} {data!r}")
        return data


PurgeTransportFactory = Callable[[ImapSettings], PurgeTransport]

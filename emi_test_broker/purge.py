# SPDX-FileCopyrightText: 2026 Aleksandr Polskiy
# SPDX-License-Identifier: Apache-2.0
"""Mailbox purge: ``STORE +FLAGS \\Deleted`` + ``EXPUNGE`` (design §7).

The manual purge is refused while any run is active. Its default scope is only mail carrying the EMI
marker; ``all`` widens it to every email in the folders. A **run-scoped** purge (AMB-79) deletes only the
mail of one run, every worker's, and may run while other runs are active.
"""

import imaplib
import re
import ssl
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Protocol

from emi_reader import ImapSettings, Security
from emi_reader.transport import quote_folder

_MARKER_HEADER = "X-EMI-Test-Id"
_RUN_HEADER = "X-EMI-Run-Id"
_IMAP_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
# A worker's run ID (design §4) or the run it belongs to: <stamp>[-w<nn>]-<token>.
_RUN_ID = re.compile(r"^(?P<stamp>\d{8}t\d{6})(?:-w\d{2,})?-(?P<token>[0-9a-f]{4})$")


def run_of(run_id: str) -> str:
    """The run a run ID belongs to, without its worker part: ``20261027t055313-w03-74f4`` -> ``20261027t055313-74f4``.

    Args:
        run_id: A worker's run ID or a run.

    Returns:
        The run.

    Raises:
        ValueError: If the text is not a run ID.
    """
    match = _RUN_ID.match(run_id.strip().lower())
    if not match:
        raise ValueError(f"{run_id!r} is not a run ID (<yyyymmddthhmmss>[-w<nn>]-<4 hex>)")
    return f"{match['stamp']}-{match['token']}"


class PurgeTransport(Protocol):
    """The few write commands a purge needs."""

    def select(self, folder: str) -> None:
        """Open a folder read-write."""

    def uid_search(self, criteria: str) -> list[int]:
        """Return the UIDs matching an IMAP search."""

    def header_values(self, uids: list[int], name: str) -> dict[int, str]:
        """One header's value per message (``""`` when it has none)."""

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
        run: Only this run's mail, every worker's (``<stamp>-<token>``, see :func:`run_of`); ``""`` for none.
    """

    dry_run: bool = False
    older_than_days: int | None = None
    everything: bool = False
    requested_by: str = ""
    run: str = ""

    def __post_init__(self) -> None:
        """Check the scope.

        Raises:
            ValueError: If a run scope is combined with ``older_than_days`` or ``everything``.
        """
        if self.run and (self.everything or self.older_than_days is not None):
            raise ValueError("a run-scoped purge takes no age limit and no 'all'")


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
        For example ``HEADER X-EMI-Test-Id "" BEFORE 19-Oct-2026``. A run scope searches its start stamp,
        which servers match as a substring (RFC 3501); :func:`run_purge` then keeps exact matches only.
    """
    if request.run:
        return f'HEADER {_RUN_HEADER} "{request.run.split("-", 1)[0]}"'
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
            if request.run and uids:
                uids = _of_run(transport, uids, request.run)
            report.counts[folder] = len(uids)
            if uids and not request.dry_run:
                transport.mark_deleted(uids)
                transport.expunge()
    finally:
        transport.close()
    return report


def _of_run(transport: PurgeTransport, uids: list[int], run: str) -> list[int]:
    """The candidates whose run marker belongs to ``run`` exactly (the search only narrowed them)."""
    kept: list[int] = []
    for uid, value in sorted(transport.header_values(uids, _RUN_HEADER).items()):
        try:
            if run_of(value) == run:
                kept.append(uid)
        except ValueError:
            continue  # someone else's marker, or none: not this run's mail
    return kept


def _literals(data: list[Any]) -> list[tuple[bytes, bytes]]:
    """The ``(envelope line, literal)`` pairs of a ``FETCH`` reply; imaplib also returns a closing ``b")"``."""
    return [(entry[0], entry[1]) for entry in data if isinstance(entry, tuple) and len(entry) == 2]


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

    def header_values(self, uids: list[int], name: str) -> dict[int, str]:
        """One header's value per message, in one ``UID FETCH`` (every fetch costs alike, design §5.9).

        Args:
            uids: Message UIDs.
            name: Header name.

        Returns:
            UID -> the header's value (``""`` when the message has none).
        """
        data = self._check(self._imap.uid("FETCH", ",".join(str(uid) for uid in uids),
                                          f"(UID BODY.PEEK[HEADER.FIELDS ({name.upper()})])"))
        values: dict[int, str] = {}
        for envelope, literal in _literals(data):
            found = re.search(rb"UID (\d+)", envelope)
            if found:
                header = literal.decode("ascii", "replace").partition(":")[2]
                values[int(found.group(1))] = " ".join(header.split())
        return values

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

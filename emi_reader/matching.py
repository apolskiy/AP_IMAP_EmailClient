# SPDX-FileCopyrightText: 2026 Aleksandr Polskiy
# SPDX-License-Identifier: Apache-2.0
"""What a wait looks for and what it found, shared by a mailbox's own loop and the shared poller (design §5.4,
§5.9).

Both decide a match the same way: exact recipient on the wait's fields, the test and run markers, then the
requested CTA. Each fetched email at the address also gets its inventory category (AMB-76).
"""

from dataclasses import dataclass, field

from emi_reader.accessibility import FolderAccess, matrix_rows
from emi_reader.errors import LinkExtractionError, MailboxAccessError
from emi_reader.inventory import CATEGORIES, Snapshot, classify
from emi_reader.keywords import ReaderCaseKeyword, ReaderKeyword
from emi_reader.links import Cta, LinkExtractor
from emi_reader.mime import ParsedEmail
from emi_reader.settings import ImapSettings
from emi_reader.transport import FolderStatus, ImapTransport

# IMAP SEARCH criteria per recipient field; the client-side check decides the final match.
SEARCH_KEYS: dict[str, tuple[str, ...]] = {
    "to": ("TO",),
    "cc": ("CC",),
    "bcc": ("BCC",),
    "delivered-to": ("HEADER", "Delivered-To"),
    "x-original-to": ("HEADER", "X-Original-To"),
}


@dataclass(frozen=True)
class ReceivedEmail:
    """One email that matched a wait.

    Attributes:
        uid: IMAP UID in its folder.
        folder: Folder it was found in.
        email: The decoded email.
        link: The CTA link, when a CTA was requested.
        secondary: Secondary keywords from extraction (e.g. no HTML part).
    """

    uid: int
    folder: str
    email: ParsedEmail
    link: str | None = None
    secondary: tuple[str, ...] = ()

    @property
    def message_id(self) -> str:
        """The ``Message-ID`` header."""
        return self.email.message_id

    @property
    def subject(self) -> str:
        """The decoded subject."""
        return self.email.subject


@dataclass
class Cursor:
    """Per-folder position: UIDs below ``next_uid`` have been looked at."""

    folder: str
    uidvalidity: int
    next_uid: int


@dataclass
class WaitState:
    """Everything one wait has found so far."""

    address: str
    cursors: list[Cursor] = field(default_factory=list)
    matches: dict[tuple[str, int], ReceivedEmail] = field(default_factory=dict)
    access_start: list[FolderAccess] = field(default_factory=list)
    folders: dict[str, dict[str, int]] = field(default_factory=dict)
    seen: dict[tuple[str, int], str] = field(default_factory=dict)
    before: Snapshot | None = None

    def snapshot(self) -> Snapshot:
        """Folder totals and the categories of every email seen at the address so far (AMB-76)."""
        counts = dict.fromkeys(CATEGORIES, 0)
        for category in self.seen.values():
            counts[category] += 1
        return Snapshot({name: dict(values) for name, values in self.folders.items()}, counts)

    def record_status(self, folder: str, status: FolderStatus) -> None:
        """Keep a folder's latest totals from an ``EXAMINE``."""
        self.folders[folder] = {"messages": status.messages, "uidnext": status.uidnext}

    def forget_folder(self, folder: str) -> None:
        """Drop a folder's matches after its ``UIDVALIDITY`` changed (its UIDs mean other messages now)."""
        self.matches = {key: value for key, value in self.matches.items() if key[0] != folder}


@dataclass(frozen=True)
class WaitTarget:
    """What a wait is looking for."""

    address: str
    cta: Cta | None
    test_id: str | None
    run_id: str | None
    match_fields: tuple[str, ...]
    expected_folder: str | None = None


def match_fields_for(requested: tuple[str, ...] | None, default: tuple[str, ...]) -> tuple[str, ...]:
    """The recipient fields for one wait: the requested ones (validated) or the mailbox default.

    Args:
        requested: Fields asked for by the wait, or None.
        default: The mailbox's fields.

    Returns:
        Lower-case field names.

    Raises:
        ValueError: On an unknown or empty selection.
    """
    fields = tuple(name.lower() for name in (default if requested is None else requested))
    unknown = [name for name in fields if name not in SEARCH_KEYS]
    if unknown or not fields:
        raise ValueError(f"match_fields must be a non-empty subset of {sorted(SEARCH_KEYS)}, got {list(requested)}")
    return fields


def case_key(test_id: str) -> str:
    """Compare test IDs in TestRail form: ``400001``, ``c400001`` and ``C400001`` are the same case.

    Args:
        test_id: A test ID in any of those forms.

    Returns:
        The ``C<number>`` form.
    """
    value = test_id.strip().upper()
    return f"C{value}" if value.isdigit() else value


def search_address(transport: ImapTransport, target: WaitTarget, since_uid: int = 0) -> list[int]:
    """Server-side search per selected field, from ``since_uid`` on; only narrows candidates.

    A search costs little however full the folder is, while each fetch does not (AMB-71, §5.9), so polls
    search for the address among new UIDs instead of fetching every new message.

    Args:
        transport: A connection with the folder selected.
        target: The wait.
        since_uid: First UID to consider; 0 for the whole folder.

    Returns:
        Candidate UIDs, ascending.
    """
    uid_range = ("UID", f"{since_uid}:*") if since_uid else ()
    uids: set[int] = set()
    for name in target.match_fields:
        uids.update(transport.uid_search((*uid_range, *SEARCH_KEYS[name], f'"{target.address}"')))
    # "n:*" also matches the highest UID when n is above it (RFC 3501), so drop anything older.
    return sorted(uid for uid in uids if uid >= since_uid)


def readable_folders(access: list[FolderAccess], settings: ImapSettings) -> list[FolderAccess]:
    """The folders a wait can read.

    Args:
        access: The latest probe.
        settings: The account (for the error's details).

    Returns:
        The accessible folders, in configured order.

    Raises:
        MailboxAccessError: ``READER_MAILBOX_NONE_ACCESSIBLE`` (environment) when there is none.
    """
    readable = [entry for entry in access if entry.accessible]
    if not readable:
        causes = tuple(dict.fromkeys(entry.detail for entry in access if entry.detail.startswith("READER_")))
        raise MailboxAccessError(
            f"none of the folders {list(settings.folders)} can be read",
            ReaderKeyword.MAILBOX_NONE_ACCESSIBLE,
            secondary=causes,
            infrastructure=True,
            details={"folders": matrix_rows(access), "endpoint": settings.endpoint},
        )
    return readable


def expected_folder_for(requested: str | None, settings: ImapSettings) -> str | None:
    """The folder a wait declares it needs, checked against the configured folders (AMB-85).

    Args:
        requested: The folder, or None.
        settings: The account.

    Returns:
        The folder as configured, or None.

    Raises:
        ValueError: If the folder is not among the configured folders (invalid input).
    """
    if requested is None:
        return None
    if requested not in settings.folders:
        raise ValueError(f"expected_folder must be one of the configured folders {list(settings.folders)}, "
                         f"got {requested!r}")
    return requested


def check_expected_folder(access: list[FolderAccess], target: WaitTarget, settings: ImapSettings) -> None:
    """Refuse a wait whose declared folder cannot be read (design §5.3, AMB-85).

    Args:
        access: The latest probe.
        target: The wait.
        settings: The account (for the error's details).

    Raises:
        MailboxAccessError: ``READER_MAILBOX_EXPECTED_FOLDER_INACCESSIBLE`` (environment).
    """
    if target.expected_folder is None:
        return
    entry = next((item for item in access if item.folder == target.expected_folder), None)
    if entry is not None and entry.accessible:
        return
    causes = (entry.detail,) if entry is not None and entry.detail.startswith("READER_") else ()
    raise MailboxAccessError(
        f"the expected folder {target.expected_folder!r} cannot be read",
        ReaderKeyword.MAILBOX_EXPECTED_FOLDER_INACCESSIBLE,
        secondary=causes,
        infrastructure=True,
        details={"folder": target.expected_folder, "folders": matrix_rows(access), "endpoint": settings.endpoint},
    )


def addressed_to(email: ParsedEmail, target: WaitTarget) -> bool:
    """Whether the email names the wait's address on one of its fields (what the wait's search found).

    Args:
        email: A fetched email.
        target: The wait.

    Returns:
        True when the address is on one of the wait's recipient fields.
    """
    return target.address in email.addresses_in(target.match_fields)


def accept(state: WaitState, target: WaitTarget, extractor: LinkExtractor, found: tuple[str, int],
           email: ParsedEmail) -> None:
    """Record a fetched email at the wait's address: its inventory category, and a match when the markers
    and the CTA fit.

    Args:
        state: The wait's state.
        target: The wait.
        extractor: Link extraction (the wait's mismatch rule).
        found: ``(folder, UID)``.
        email: The parsed email.

    Raises:
        LinkExtractionError: A matching email's link is ambiguous or inconsistent.
    """
    state.seen[found] = classify(email)  # the inventory, from what the wait fetches anyway
    if not _markers_match(email, target):
        return
    folder, uid = found
    if target.cta is None:
        state.matches[found] = ReceivedEmail(uid=uid, folder=folder, email=email)
        return
    try:
        result = extractor.extract(email, target.cta)
    except LinkExtractionError as error:
        if error.primary == ReaderCaseKeyword.LINK_NOT_FOUND:
            return  # another kind of email to the same address: not a match, not an error
        raise
    state.matches[found] = ReceivedEmail(uid=uid, folder=folder, email=email, link=result.url,
                                         secondary=result.secondary)


def _markers_match(email: ParsedEmail, target: WaitTarget) -> bool:
    """Exact, case-insensitive recipient match on the selected fields, plus the markers."""
    if not addressed_to(email, target):
        return False
    if target.test_id is not None and case_key(email.test_id) != case_key(target.test_id):
        return False
    return target.run_id is None or email.run_id == target.run_id

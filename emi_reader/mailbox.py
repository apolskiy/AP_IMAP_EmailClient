# SPDX-FileCopyrightText: 2026 Aleksandr Polskiy
# SPDX-License-Identifier: Apache-2.0
"""Wait for a test's email in the shared mailbox and return it or its CTA link (design §5.4, §5.5)."""

import logging
import random
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, NoReturn

from emi_reader.accessibility import FolderAccess, filter_evidence, matrix_rows, probe_account
from emi_reader.errors import EmailWaitError, ImapConnectionError, LinkExtractionError, MailboxAccessError
from emi_reader.inventory import CATEGORIES, Inventory, Snapshot, classify
from emi_reader.keywords import ReaderCaseKeyword, ReaderKeyword
from emi_reader.links import Cta, LinkExtractor
from emi_reader.mime import ParsedEmail, parse_email
from emi_reader.settings import ImapSettings
from emi_reader.transport import FolderStatus, ImaplibTransport, ImapTransport

_LOGGER = logging.getLogger(__name__)

# IMAP SEARCH criteria per recipient field; the client-side check decides the final match.
_SEARCH_KEYS: dict[str, tuple[str, ...]] = {
    "to": ("TO",),
    "cc": ("CC",),
    "bcc": ("BCC",),
    "delivered-to": ("HEADER", "Delivered-To"),
    "x-original-to": ("HEADER", "X-Original-To"),
}


@dataclass(frozen=True, kw_only=True)
class WaitOptions:  # pylint: disable=too-many-instance-attributes
    """Tunable behaviour of a wait; every value is customizable (design §10).

    Attributes:
        poll_interval: Seconds between polls.
        poll_jitter: Random seconds added to each poll interval (0..jitter).
        settle_window: Seconds to keep watching for extra emails once the expected count arrived.
        match_fields: Recipient fields the address must appear in (default ``To`` only).
        mismatch_is_error: Fail when HTML and text links differ (else only warn).
        filter_risk_folders: Folders declared as possible filter targets.
        backoff: Reconnect delays in seconds; the last value repeats.
    """

    poll_interval: float = 2.0
    poll_jitter: float = 0.5
    settle_window: float = 2.0
    match_fields: tuple[str, ...] = ("to",)
    mismatch_is_error: bool = True
    filter_risk_folders: tuple[str, ...] = ()
    backoff: tuple[float, ...] = (0.5, 1.0, 2.0, 4.0, 5.0)

    def __post_init__(self) -> None:
        """Reject unknown recipient fields and negative timings.

        Raises:
            ValueError: On an invalid value.
        """
        unknown = [name for name in self.match_fields if name.lower() not in _SEARCH_KEYS]
        if unknown or not self.match_fields:
            raise ValueError(f"match_fields must be a non-empty subset of {sorted(_SEARCH_KEYS)}, "
                             f"got {list(self.match_fields)}")
        if min(self.poll_interval, self.poll_jitter, self.settle_window, *self.backoff) < 0:
            raise ValueError("timings must not be negative")


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


@dataclass(frozen=True)
class RawEmail:
    """One email as stored, for a failed test's report (design §9.8, AMB-74).

    Attributes:
        uid: IMAP UID in its folder.
        folder: Folder it was found in.
        address: The test address it was found for.
        subject: The decoded subject.
        raw: The email's bytes as fetched.
    """

    uid: int
    folder: str
    address: str
    subject: str
    raw: bytes = field(repr=False)


@dataclass
class _Cursor:
    """Per-folder position: UIDs below ``next_uid`` have been looked at."""

    folder: str
    uidvalidity: int
    next_uid: int


@dataclass
class _WaitState:
    """Everything one wait has found so far."""

    address: str
    cursors: list[_Cursor] = field(default_factory=list)
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
        """Keep a folder's latest totals from the wait's own ``EXAMINE``."""
        self.folders[folder] = {"messages": status.messages, "uidnext": status.uidnext}


@dataclass(frozen=True)
class _Target:
    """What a wait is looking for."""

    address: str
    cta: Cta | None
    test_id: str | None
    run_id: str | None
    match_fields: tuple[str, ...]


def _match_fields(requested: tuple[str, ...] | None, default: tuple[str, ...]) -> tuple[str, ...]:
    """The recipient fields for one wait: the requested ones (validated) or the mailbox default."""
    fields = tuple(name.lower() for name in (default if requested is None else requested))
    unknown = [name for name in fields if name not in _SEARCH_KEYS]
    if unknown or not fields:
        raise ValueError(f"match_fields must be a non-empty subset of {sorted(_SEARCH_KEYS)}, got {list(requested)}")
    return fields


def _case_key(test_id: str) -> str:
    """Compare test IDs in TestRail form: ``400001``, ``c400001`` and ``C400001`` are the same case."""
    value = test_id.strip().upper()
    return f"C{value}" if value.isdigit() else value


class Mailbox:
    """Read-only access to the shared mailbox through one IMAP connection.

    The reader never changes flags or deletes mail: folders are opened with ``EXAMINE`` and messages
    fetched with ``BODY.PEEK``. A wait searches immediately (pre-arrived mail) and then fetches only
    new UIDs on each poll.
    """

    def __init__(
        self,
        settings: ImapSettings,
        *,
        options: WaitOptions | None = None,
        transport_factory: Callable[[ImapSettings], ImapTransport] = ImaplibTransport,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
        rng: random.Random | None = None,
    ) -> None:
        """Create a mailbox; the connection opens on first use.

        Args:
            settings: Connection settings.
            options: Wait behaviour; defaults per design §10.
            transport_factory: Builds a transport (a simulated one in unit tests).
            clock: Monotonic clock, injectable for tests.
            sleeper: Sleep function, injectable for tests.
            rng: Random source for poll jitter.
        """
        self._settings = settings
        self._options = options or WaitOptions()
        self._factory = transport_factory
        self._clock = clock
        self._sleeper = sleeper
        self._rng = rng or random.Random()
        self._transport: ImapTransport | None = None
        self._extractor = LinkExtractor(mismatch_is_error=self._options.mismatch_is_error)
        self._last_access: list[FolderAccess] = []
        self._attempts: list[dict[str, Any]] = []
        self._inventory: dict[str, Any] | None = None

    @property
    def access_matrix(self) -> list[dict[str, Any]]:
        """Accessibility of each folder at the latest probe, for the report."""
        return matrix_rows(self._last_access)

    @property
    def attempts(self) -> list[dict[str, Any]]:
        """Network failures retried during the latest wait."""
        return list(self._attempts)

    @property
    def inventory(self) -> dict[str, Any] | None:
        """The latest wait's mailbox inventory as JSON (design §5.8), or None before any wait got started."""
        return self._inventory

    def close(self) -> None:
        """Close the connection; safe to call more than once."""
        if self._transport is not None:
            self._transport.close()
            self._transport = None

    def _connection(self) -> ImapTransport:
        """Return the open transport, opening it if needed."""
        if self._transport is None:
            self._transport = self._factory(self._settings)
        self._transport.open()
        return self._transport

    def wait_for_emails(
        self,
        address: str,
        *,
        cta: Cta | None = None,
        count: int = 1,
        timeout: float = 60.0,
        test_id: str | None = None,
        run_id: str | None = None,
        match_fields: tuple[str, ...] | None = None,
        settle_window: float | None = None,
    ) -> list[ReceivedEmail]:
        """Wait until exactly ``count`` matching emails are present.

        Args:
            address: The test's own address.
            cta: Only emails containing this button count; ``None`` accepts any email.
            count: Number of emails expected (at least 1).
            timeout: Seconds to wait.
            test_id: Required ``X-EMI-Test-Id`` marker (sender-generated mail).
            run_id: Required ``X-EMI-Run-Id`` marker.
            match_fields: Recipient fields for this wait (``to``, ``cc``, ``bcc``, ``delivered-to``,
                ``x-original-to``); the mailbox's ``WaitOptions.match_fields`` by default.
            settle_window: Seconds to keep watching for extra emails once ``count`` arrived; the mailbox's
                ``WaitOptions.settle_window`` by default. 0 suits mail that was fully delivered before the wait.

        Returns:
            The matching emails, oldest first.

        Raises:
            EmailWaitError: ``CASE_EMAIL_NOT_RECEIVED`` or ``CASE_EMAIL_COUNT_EXCEEDED``.
            MailboxAccessError: No readable folder, or evidence of filtering (environment).
            ImapConnectionError: A network fault that retries did not cure, or a fatal one.
            LinkExtractionError: A matching email's link is ambiguous or inconsistent.
        """
        if count < 1:
            raise ValueError(f"count must be at least 1, got {count}")
        settle = self._options.settle_window if settle_window is None else float(settle_window)
        if settle < 0:
            raise ValueError(f"settle_window must not be negative, got {settle}")
        target = _Target(address.strip().lower(), cta, test_id, run_id,
                         _match_fields(match_fields, self._options.match_fields))
        found = self._wait(target, timeout, lambda matches: len(matches) >= count, settle=settle)
        if len(found) > count:
            raise EmailWaitError(
                f"{len(found)} matching emails for {target.address}, expected {count}",
                ReaderCaseKeyword.EMAIL_COUNT_EXCEEDED,
                details={"address": target.address, "expected": count, "found": len(found),
                         "message_ids": [item.message_id for item in found], "inventory": self._inventory},
            )
        return found

    def wait_for_link(
        self,
        address: str,
        cta: Cta,
        *,
        timeout: float = 60.0,
        test_id: str | None = None,
        run_id: str | None = None,
        match_fields: tuple[str, ...] | None = None,
    ) -> str:
        """Wait for an email with the given button and return its link (newest if several).

        Args:
            address: The test's own address.
            cta: The button whose link is wanted.
            timeout: Seconds to wait.
            test_id: Required ``X-EMI-Test-Id`` marker (sender-generated mail).
            run_id: Required ``X-EMI-Run-Id`` marker.
            match_fields: Recipient fields for this wait; the mailbox default when None.

        Returns:
            The CTA link.
        """
        target = _Target(address.strip().lower(), cta, test_id, run_id,
                         _match_fields(match_fields, self._options.match_fields))
        found = self._wait(target, timeout, lambda matches: len(matches) >= 1, settle=0.0)
        if len(found) > 1:
            _LOGGER.warning("%s: %d emails for %s; using the newest",
                            ReaderCaseKeyword.DUPLICATE_DELIVERY, len(found), target.address)
        newest = found[-1]
        if newest.link is None:  # cannot happen: a CTA was requested, so every match has a link
            raise LinkExtractionError("matched email has no link", ReaderCaseKeyword.LINK_NOT_FOUND)
        return newest.link

    def emails_to(self, addresses: tuple[str, ...], limit: int = 30) -> list[RawEmail]:
        """Every email addressed to any of the addresses, matched or not, for a failed test's report.

        Searches every readable folder on all recipient fields, so emails a wait skipped (another kind, a
        wrong marker) and emails that arrived after it gave up are included. Never used for pass/fail.

        Args:
            addresses: The test's addresses.
            limit: Most emails returned.

        Returns:
            The newest ``limit`` emails, newest first (per folder by UID, folders in configured order).
        """
        transport = self._connection()
        wanted = tuple(dict.fromkeys(address.strip().lower() for address in addresses if address.strip()))
        newest = sorted(self._located(transport, wanted), key=lambda item: item[1], reverse=True)[:max(limit, 0)]
        emails: list[RawEmail] = []
        for folder in dict.fromkeys(item[0] for item in newest):
            chosen = {uid: address for name, uid, address in newest if name == folder}
            transport.examine(folder)
            for uid, raw in sorted(transport.fetch_messages(sorted(chosen)).items(), reverse=True):
                emails.append(RawEmail(uid=uid, folder=folder, address=chosen[uid],
                                       subject=parse_email(raw).subject, raw=raw))
        return emails

    def _located(self, transport: ImapTransport, addresses: tuple[str, ...]) -> list[tuple[str, int, str]]:
        """``(folder, UID, address)`` of every email to the addresses, on all recipient fields, readable folders."""
        access = probe_account(transport, self._settings.username, self._settings.folders)
        self._last_access = access
        found: list[tuple[str, int, str]] = []
        for entry in (item for item in access if item.accessible):
            transport.examine(entry.folder)
            seen: set[int] = set()
            for address in addresses:
                target = _Target(address, None, None, None, tuple(_SEARCH_KEYS))
                uids = [uid for uid in self._search_address(transport, target) if uid not in seen]
                seen.update(uids)
                found.extend((entry.folder, uid, address) for uid in uids)
        return found

    def _wait(
        self,
        target: _Target,
        timeout: float,
        satisfied: Callable[[list[ReceivedEmail]], bool],
        *,
        settle: float,
    ) -> list[ReceivedEmail]:
        """Poll until ``satisfied`` (plus ``settle`` seconds), the deadline, or a fatal error."""
        deadline = self._clock() + timeout
        settle_until: float | None = None
        state: _WaitState | None = None
        last_error: ImapConnectionError | None = None
        self._attempts = []
        self._inventory = None
        while True:
            try:
                state = self._start(target) if state is None else state
                self._poll(state, target)
                last_error = None
            except ImapConnectionError as error:
                if not error.retryable:
                    raise
                last_error = self._record_retry(error)
            if state is not None and state.before is not None:
                self._inventory = Inventory(target.address, target.match_fields, state.before,
                                            state.snapshot()).to_json()
            found = sorted(state.matches.values(), key=lambda item: (item.folder, item.uid)) if state else []
            now = self._clock()
            if satisfied(found):
                if settle <= 0:
                    return found
                settle_until = settle_until if settle_until is not None else now + settle
                if now >= settle_until:
                    return found
            if now >= deadline:
                if satisfied(found):
                    return found  # the deadline cut the settle window short
                self._give_up(target, state, last_error, found)
            self._sleeper(min(self._next_delay(last_error), max(deadline - now, 0.0)))

    def _record_retry(self, error: ImapConnectionError) -> ImapConnectionError:
        """Remember a healable failure and drop the connection so the next poll reconnects."""
        self._attempts.append({"keyword": error.primary, "message": str(error), "at": self._clock()})
        _LOGGER.warning("retrying after %s: %s", error.primary, error)
        self.close()
        return error

    def _next_delay(self, last_error: ImapConnectionError | None) -> float:
        """Poll interval with jitter, or the next backoff step after a failure."""
        if last_error is not None:
            steps = self._options.backoff
            return steps[min(len(self._attempts) - 1, len(steps) - 1)]
        return self._options.poll_interval + self._rng.uniform(0.0, self._options.poll_jitter)

    def _give_up(
        self,
        target: _Target,
        state: _WaitState | None,
        last_error: ImapConnectionError | None,
        found: list[ReceivedEmail],
    ) -> NoReturn:
        """Decide the failure at the deadline: network fault, filtering, or email not received.

        An outage is never reported as "email not received": if the last attempt failed on the
        network, that network keyword is the primary one.

        Raises:
            ImapConnectionError: The last attempt failed on the network.
            MailboxAccessError: Mail may have been filtered to an unreadable folder.
            EmailWaitError: Fewer matching emails than expected arrived.
        """
        if last_error is not None:
            raise last_error.with_details(attempts=self.attempts)
        if state is not None:
            self._raise_if_filtered(state)
        raise EmailWaitError(
            f"{len(found)} matching email(s) for {target.address} before the deadline",
            ReaderCaseKeyword.EMAIL_NOT_RECEIVED,
            details={"address": target.address, "cta": target.cta.label if target.cta else None,
                     "found": len(found), "folders": self.access_matrix, "inventory": self._inventory},
        )

    def _raise_if_filtered(self, state: _WaitState) -> None:
        """Re-probe folders; raise an environment failure if mail may be filtered away."""
        after = probe_account(self._connection(), self._settings.username, self._settings.folders)
        self._last_access = after
        signals = filter_evidence(state.access_start, after, self._options.filter_risk_folders)
        if signals:
            raise MailboxAccessError(
                f"email for {state.address} not found; mail may be filtered to an unreadable folder",
                ReaderKeyword.MAILBOX_FILTERED_TO_INACCESSIBLE,
                infrastructure=True,
                details={"signals": signals, "folders": self.access_matrix},
            )

    def _start(self, target: _Target) -> _WaitState:
        """Probe folders and search for mail that arrived before the wait started."""
        transport = self._connection()
        access = probe_account(transport, self._settings.username, self._settings.folders,
                               raise_retryable=True)
        self._last_access = access
        readable = [entry for entry in access if entry.accessible]
        if not readable:
            causes = tuple(dict.fromkeys(entry.detail for entry in access
                                         if entry.detail.startswith("READER_")))
            raise MailboxAccessError(
                f"none of the folders {list(self._settings.folders)} can be read",
                ReaderKeyword.MAILBOX_NONE_ACCESSIBLE,
                secondary=causes,
                infrastructure=True,
                details={"folders": self.access_matrix, "endpoint": self._settings.endpoint},
            )
        state = _WaitState(address=target.address, access_start=access)
        for entry in readable:
            status = transport.examine(entry.folder)
            state.record_status(entry.folder, status)
            cursor = _Cursor(entry.folder, status.uidvalidity, status.uidnext)
            state.cursors.append(cursor)
            self._consider(state, target, cursor.folder, self._search_address(transport, target))
        state.before = state.snapshot()
        return state

    def _search_address(self, transport: ImapTransport, target: _Target, since_uid: int = 0) -> list[int]:
        """Server-side search per selected field, from ``since_uid`` on; only narrows candidates.

        A search costs about the same however full the folder is, while each fetch does not (AMB-71),
        so polls search for the address among new UIDs instead of fetching every new message.
        """
        uid_range = ("UID", f"{since_uid}:*") if since_uid else ()
        uids: set[int] = set()
        for name in target.match_fields:
            uids.update(transport.uid_search((*uid_range, *_SEARCH_KEYS[name], f'"{target.address}"')))
        # "n:*" also matches the highest UID when n is above it (RFC 3501), so drop anything older.
        return sorted(uid for uid in uids if uid >= since_uid)

    def _poll(self, state: _WaitState, target: _Target) -> None:
        """Fetch and check UIDs that arrived since the last look, in every readable folder."""
        transport = self._connection()
        for cursor in state.cursors:
            status = transport.examine(cursor.folder)
            state.record_status(cursor.folder, status)
            if status.uidvalidity != cursor.uidvalidity:
                _LOGGER.info("%s: %s %d -> %d; searching again", ReaderKeyword.IMAP_UIDVALIDITY_CHANGED,
                             cursor.folder, cursor.uidvalidity, status.uidvalidity)
                state.matches = {
                    key: value for key, value in state.matches.items() if key[0] != cursor.folder
                }
                cursor.uidvalidity, cursor.next_uid = status.uidvalidity, status.uidnext
                self._consider(state, target, cursor.folder, self._search_address(transport, target))
                continue
            if status.uidnext <= cursor.next_uid:
                continue
            new_uids = self._search_address(transport, target, cursor.next_uid)
            cursor.next_uid = status.uidnext
            self._consider(state, target, cursor.folder, new_uids)

    def _consider(self, state: _WaitState, target: _Target, folder: str, uids: list[int]) -> None:
        """Fetch the candidate UIDs whole, in one command, and record those matching address, marker and CTA."""
        candidates = [uid for uid in uids if (folder, uid) not in state.matches]
        if not candidates:
            return
        for uid, raw in sorted(self._connection().fetch_messages(candidates).items()):
            email = parse_email(raw)
            state.seen[(folder, uid)] = classify(email)  # the inventory, from what the wait fetches anyway
            if not self._header_match(email, target):
                continue
            match = self._full_match(email, uid, folder, target)
            if match is not None:
                state.matches[(folder, uid)] = match

    def _header_match(self, headers: ParsedEmail, target: _Target) -> bool:
        """Exact, case-insensitive recipient match on the selected fields, plus the markers."""
        if target.address not in headers.addresses_in(target.match_fields):
            return False
        if target.test_id is not None and _case_key(headers.test_id) != _case_key(target.test_id):
            return False
        return target.run_id is None or headers.run_id == target.run_id

    def _full_match(self, email: ParsedEmail, uid: int, folder: str, target: _Target) -> ReceivedEmail | None:
        """Check a whole message contains the requested CTA."""
        if target.cta is None:
            return ReceivedEmail(uid=uid, folder=folder, email=email)
        try:
            result = self._extractor.extract(email, target.cta)
        except LinkExtractionError as error:
            if error.primary == ReaderCaseKeyword.LINK_NOT_FOUND:
                return None  # another kind of email to the same address: not a match, not an error
            raise
        return ReceivedEmail(uid=uid, folder=folder, email=email, link=result.url,
                             secondary=result.secondary)

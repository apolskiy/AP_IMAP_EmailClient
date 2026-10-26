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
from emi_reader.keywords import ReaderCaseKeyword, ReaderKeyword
from emi_reader.links import Cta, LinkExtractor
from emi_reader.mime import ParsedEmail, parse_email
from emi_reader.settings import ImapSettings
from emi_reader.transport import ImaplibTransport, ImapTransport

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


@dataclass(frozen=True)
class _Target:
    """What a wait is looking for."""

    address: str
    cta: Cta | None
    test_id: str | None
    run_id: str | None


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

    @property
    def access_matrix(self) -> list[dict[str, Any]]:
        """Accessibility of each folder at the latest probe, for the report."""
        return matrix_rows(self._last_access)

    @property
    def attempts(self) -> list[dict[str, Any]]:
        """Network failures retried during the latest wait."""
        return list(self._attempts)

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
    ) -> list[ReceivedEmail]:
        """Wait until exactly ``count`` matching emails are present.

        Args:
            address: The test's own address.
            cta: Only emails containing this button count; ``None`` accepts any email.
            count: Number of emails expected (at least 1).
            timeout: Seconds to wait.
            test_id: Required ``X-EMI-Test-Id`` marker (sender-generated mail).
            run_id: Required ``X-EMI-Run-Id`` marker.

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
        target = _Target(address.strip().lower(), cta, test_id, run_id)
        found = self._wait(target, timeout, lambda matches: len(matches) >= count, settle=True)
        if len(found) > count:
            raise EmailWaitError(
                f"{len(found)} matching emails for {target.address}, expected {count}",
                ReaderCaseKeyword.EMAIL_COUNT_EXCEEDED,
                details={"address": target.address, "expected": count, "found": len(found),
                         "message_ids": [item.message_id for item in found]},
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
    ) -> str:
        """Wait for an email with the given button and return its link (newest if several).

        Args:
            address: The test's own address.
            cta: The button whose link is wanted.
            timeout: Seconds to wait.
            test_id: Required ``X-EMI-Test-Id`` marker (sender-generated mail).
            run_id: Required ``X-EMI-Run-Id`` marker.

        Returns:
            The CTA link.
        """
        target = _Target(address.strip().lower(), cta, test_id, run_id)
        found = self._wait(target, timeout, lambda matches: len(matches) >= 1, settle=False)
        if len(found) > 1:
            _LOGGER.warning("%s: %d emails for %s; using the newest",
                            ReaderCaseKeyword.DUPLICATE_DELIVERY, len(found), target.address)
        newest = found[-1]
        if newest.link is None:  # cannot happen: a CTA was requested, so every match has a link
            raise LinkExtractionError("matched email has no link", ReaderCaseKeyword.LINK_NOT_FOUND)
        return newest.link

    def _wait(
        self,
        target: _Target,
        timeout: float,
        satisfied: Callable[[list[ReceivedEmail]], bool],
        *,
        settle: bool,
    ) -> list[ReceivedEmail]:
        """Poll until ``satisfied`` (plus the settle window), the deadline, or a fatal error."""
        deadline = self._clock() + timeout
        settle_until: float | None = None
        state: _WaitState | None = None
        last_error: ImapConnectionError | None = None
        self._attempts = []
        while True:
            try:
                state = self._start(target) if state is None else state
                self._poll(state, target)
                last_error = None
            except ImapConnectionError as error:
                if not error.retryable:
                    raise
                last_error = self._record_retry(error)
            found = sorted(state.matches.values(), key=lambda item: (item.folder, item.uid)) if state else []
            now = self._clock()
            if satisfied(found):
                if not settle:
                    return found
                settle_until = settle_until if settle_until is not None else now + self._options.settle_window
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
                     "found": len(found), "folders": self.access_matrix},
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
            cursor = _Cursor(entry.folder, status.uidvalidity, status.uidnext)
            state.cursors.append(cursor)
            self._consider(state, target, cursor.folder, self._search_address(transport, target.address))
        return state

    def _search_address(self, transport: ImapTransport, address: str) -> list[int]:
        """Server-side search per selected field; only narrows candidates."""
        uids: set[int] = set()
        for name in self._options.match_fields:
            uids.update(transport.uid_search((*_SEARCH_KEYS[name.lower()], f'"{address}"')))
        return sorted(uids)

    def _poll(self, state: _WaitState, target: _Target) -> None:
        """Fetch and check UIDs that arrived since the last look, in every readable folder."""
        transport = self._connection()
        for cursor in state.cursors:
            status = transport.examine(cursor.folder)
            if status.uidvalidity != cursor.uidvalidity:
                _LOGGER.info("%s: %s %d -> %d; searching again", ReaderKeyword.IMAP_UIDVALIDITY_CHANGED,
                             cursor.folder, cursor.uidvalidity, status.uidvalidity)
                state.matches = {
                    key: value for key, value in state.matches.items() if key[0] != cursor.folder
                }
                cursor.uidvalidity, cursor.next_uid = status.uidvalidity, status.uidnext
                self._consider(state, target, cursor.folder, self._search_address(transport, target.address))
                continue
            if status.uidnext <= cursor.next_uid:
                continue
            new_uids = [uid for uid in transport.uid_search(("UID", f"{cursor.next_uid}:*"))
                        if uid >= cursor.next_uid]
            cursor.next_uid = status.uidnext
            self._consider(state, target, cursor.folder, new_uids)

    def _consider(self, state: _WaitState, target: _Target, folder: str, uids: list[int]) -> None:
        """Check candidate UIDs against address, marker and CTA; record matches."""
        candidates = [uid for uid in uids if (folder, uid) not in state.matches]
        if not candidates:
            return
        transport = self._connection()
        for uid, header_bytes in transport.fetch_headers(candidates).items():
            headers = parse_email(header_bytes)
            if not self._header_match(headers, target):
                continue
            match = self._full_match(transport, uid, folder, target)
            if match is not None:
                state.matches[(folder, uid)] = match

    def _header_match(self, headers: ParsedEmail, target: _Target) -> bool:
        """Exact, case-insensitive recipient match on the selected fields, plus the markers."""
        if target.address not in headers.addresses_in(self._options.match_fields):
            return False
        if target.test_id is not None and _case_key(headers.test_id) != _case_key(target.test_id):
            return False
        return target.run_id is None or headers.run_id == target.run_id

    def _full_match(
        self, transport: ImapTransport, uid: int, folder: str, target: _Target
    ) -> ReceivedEmail | None:
        """Fetch the whole message and check it contains the requested CTA."""
        email = parse_email(transport.fetch_message(uid))
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

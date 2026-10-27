# SPDX-FileCopyrightText: 2026 Aleksandr Polskiy
# SPDX-License-Identifier: Apache-2.0
"""Wait for a test's email in the shared mailbox and return it or its CTA link (design §5.4, §5.5, §5.9)."""

import logging
import random
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, NoReturn

from emi_reader.accessibility import FolderAccess, filter_evidence, matrix_rows, probe_account
from emi_reader.errors import EmailWaitError, ImapConnectionError, LinkExtractionError, MailboxAccessError
from emi_reader.inventory import Inventory
from emi_reader.keywords import ReaderCaseKeyword, ReaderKeyword
from emi_reader.links import Cta, LinkExtractor
from emi_reader.matching import (
    SEARCH_KEYS,
    Cursor,
    ReceivedEmail,
    WaitState,
    WaitTarget,
    accept,
    check_expected_folder,
    expected_folder_for,
    match_fields_for,
    readable_folders,
    search_address,
)
from emi_reader.mime import parse_email
from emi_reader.poller import SharedPoller
from emi_reader.settings import ImapSettings
from emi_reader.transport import ImaplibTransport, ImapTransport

_LOGGER = logging.getLogger(__name__)

__all__ = ["Mailbox", "RawEmail", "ReceivedEmail", "WaitOptions"]


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
        unknown = [name for name in self.match_fields if name.lower() not in SEARCH_KEYS]
        if unknown or not self.match_fields:
            raise ValueError(f"match_fields must be a non-empty subset of {sorted(SEARCH_KEYS)}, "
                             f"got {list(self.match_fields)}")
        if min(self.poll_interval, self.poll_jitter, self.settle_window, *self.backoff) < 0:
            raise ValueError("timings must not be negative")


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


class Mailbox:  # pylint: disable=too-many-instance-attributes
    """Read-only access to the shared mailbox.

    The reader never changes flags or deletes mail: folders are opened with ``EXAMINE`` and messages
    fetched with ``BODY.PEEK``. A wait searches immediately (pre-arrived mail) and then fetches only
    new UIDs on each poll: on the mailbox's own connection, or through a :class:`SharedPoller` that
    serves every waiting request of the account (design §5.9).
    """

    def __init__(  # pylint: disable=too-many-arguments
        self,
        settings: ImapSettings,
        *,
        options: WaitOptions | None = None,
        transport_factory: Callable[[ImapSettings], ImapTransport] = ImaplibTransport,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
        rng: random.Random | None = None,
        poller: SharedPoller | None = None,
    ) -> None:
        """Create a mailbox; the connection opens on first use.

        Args:
            settings: Connection settings.
            options: Wait behaviour; defaults per design §10.
            transport_factory: Builds a transport (a simulated one in unit tests).
            clock: Monotonic clock, injectable for tests.
            sleeper: Sleep function, injectable for tests.
            rng: Random source for poll jitter.
            poller: Serves the waits instead of this mailbox's own loop; ``emails_to`` keeps its own
                connection.
        """
        self._settings = settings
        self._options = options or WaitOptions()
        self._factory = transport_factory
        self._clock = clock
        self._sleeper = sleeper
        self._rng = rng or random.Random()
        self._poller = poller
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

    def wait_for_emails(  # pylint: disable=too-many-arguments
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
        expected_folder: str | None = None,
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
            expected_folder: A configured folder the test needs (e.g. ``Spam``); the wait ends at once when
                it cannot be read (AMB-85).

        Returns:
            The matching emails, oldest first.

        Raises:
            ValueError: An invalid count, settle window, match field or expected folder.
            EmailWaitError: ``CASE_EMAIL_NOT_RECEIVED`` or ``CASE_EMAIL_COUNT_EXCEEDED``.
            MailboxAccessError: No readable folder, the expected folder unreadable, or evidence of filtering
                (environment).
            ImapConnectionError: A network fault that retries did not cure, or a fatal one.
            LinkExtractionError: A matching email's link is ambiguous or inconsistent.
        """
        if count < 1:
            raise ValueError(f"count must be at least 1, got {count}")
        settle = self._options.settle_window if settle_window is None else float(settle_window)
        if settle < 0:
            raise ValueError(f"settle_window must not be negative, got {settle}")
        target = WaitTarget(address.strip().lower(), cta, test_id, run_id,
                            match_fields_for(match_fields, self._options.match_fields),
                            expected_folder_for(expected_folder, self._settings))
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
        expected_folder: str | None = None,
    ) -> str:
        """Wait for an email with the given button and return its link (newest if several).

        Args:
            address: The test's own address.
            cta: The button whose link is wanted.
            timeout: Seconds to wait.
            test_id: Required ``X-EMI-Test-Id`` marker (sender-generated mail).
            run_id: Required ``X-EMI-Run-Id`` marker.
            match_fields: Recipient fields for this wait; the mailbox default when None.
            expected_folder: A configured folder the test needs; unreadable ends the wait at once (AMB-85).

        Returns:
            The CTA link.
        """
        target = WaitTarget(address.strip().lower(), cta, test_id, run_id,
                            match_fields_for(match_fields, self._options.match_fields),
                            expected_folder_for(expected_folder, self._settings))
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
                target = WaitTarget(address, None, None, None, tuple(SEARCH_KEYS))
                uids = [uid for uid in search_address(transport, target) if uid not in seen]
                seen.update(uids)
                found.extend((entry.folder, uid, address) for uid in uids)
        return found

    # --- waiting ---------------------------------------------------------------------------------------

    def _wait(
        self,
        target: WaitTarget,
        timeout: float,
        satisfied: Callable[[list[ReceivedEmail]], bool],
        *,
        settle: float,
    ) -> list[ReceivedEmail]:
        """Poll until ``satisfied`` (plus ``settle`` seconds), the deadline, or a fatal error."""
        self._attempts = []
        self._inventory = None
        rule = _Rule(self._clock() + timeout, satisfied, settle)
        if self._poller is not None:
            return self._wait_shared(self._poller, target, rule)
        state: WaitState | None = None
        last_error: ImapConnectionError | None = None
        had_session = False  # a working connection during this wait (AMB-78)
        while True:
            try:
                state = self._start(target) if state is None else state
                self._poll(state, target)
                last_error, had_session = None, True
            except ImapConnectionError as error:
                if not (error.retryable or self._restarting(error, had_session)):
                    raise
                had_session = had_session or error.retryable  # only a live session can drop or time out
                last_error = self._record_retry(error)
            found = self._decide(target, state, last_error, rule)
            if found is not None:
                return found
            remaining = max(rule.deadline - self._clock(), 0.0)
            self._sleeper(min(self._next_delay(last_error), remaining))

    def _wait_shared(self, poller: SharedPoller, target: WaitTarget, rule: "_Rule") -> list[ReceivedEmail]:
        """Let the shared poller look; decide after each of its cycles by this wait's own rules (§5.9)."""
        request = poller.register(target, self._extractor)
        try:
            while True:
                poller.next_cycle(request, max(rule.deadline - self._clock(), 0.0))
                self._attempts = list(request.attempts)
                self._last_access = request.access or self._last_access
                found = self._decide(target, request.state, request.last_error, rule)
                if found is not None:
                    return found
        finally:
            poller.unregister(request)

    def _decide(self, target: WaitTarget, state: WaitState | None, last_error: ImapConnectionError | None,
                rule: "_Rule") -> list[ReceivedEmail] | None:
        """The matches when the wait is done (count reached and settled), None to look again.

        Raises:
            ImapConnectionError: At the deadline, when the last look failed on the network.
            MailboxAccessError: At the deadline, when mail may have been filtered away.
            EmailWaitError: At the deadline, when too few emails matched.
        """
        if state is not None and state.before is not None:
            self._inventory = Inventory(target.address, target.match_fields, state.before,
                                        state.snapshot()).to_json()
        found = sorted(state.matches.values(), key=lambda item: (item.folder, item.uid)) if state else []
        now = self._clock()
        if rule.satisfied(found):
            if rule.settle <= 0:
                return found
            rule.settle_until = rule.settle_until if rule.settle_until is not None else now + rule.settle
            if now >= rule.settle_until:
                return found
        if now >= rule.deadline:
            if rule.satisfied(found):
                return found  # the deadline cut the settle window short
            self._give_up(target, state, last_error, found)
        return None

    @staticmethod
    def _restarting(error: ImapConnectionError, had_session: bool) -> bool:
        """A refused reconnect after this wait had a working connection: most likely a server restart, so
        it is retried inside the deadline (AMB-78). A refused first connect stays fatal."""
        return had_session and error.primary == ReaderKeyword.IMAP_CONNECTION_REFUSED

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
        target: WaitTarget,
        state: WaitState | None,
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

    def _raise_if_filtered(self, state: WaitState) -> None:
        """Re-probe folders; raise an environment failure if mail may be filtered away."""
        if self._poller is not None:
            after = self._poller.probe()
        else:
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

    def _start(self, target: WaitTarget) -> WaitState:
        """Probe folders and search for mail that arrived before the wait started."""
        transport = self._connection()
        access = probe_account(transport, self._settings.username, self._settings.folders,
                               raise_retryable=True)
        self._last_access = access
        check_expected_folder(access, target, self._settings)
        state = WaitState(address=target.address, access_start=access)
        for entry in readable_folders(access, self._settings):
            status = transport.examine(entry.folder)
            state.record_status(entry.folder, status)
            cursor = Cursor(entry.folder, status.uidvalidity, status.uidnext)
            state.cursors.append(cursor)
            self._consider(state, target, cursor.folder, search_address(transport, target))
        state.before = state.snapshot()
        return state

    def _poll(self, state: WaitState, target: WaitTarget) -> None:
        """Fetch and check UIDs that arrived since the last look, in every readable folder."""
        transport = self._connection()
        for cursor in state.cursors:
            status = transport.examine(cursor.folder)
            state.record_status(cursor.folder, status)
            if status.uidvalidity != cursor.uidvalidity:
                _LOGGER.info("%s: %s %d -> %d; searching again", ReaderKeyword.IMAP_UIDVALIDITY_CHANGED,
                             cursor.folder, cursor.uidvalidity, status.uidvalidity)
                state.forget_folder(cursor.folder)
                cursor.uidvalidity, cursor.next_uid = status.uidvalidity, status.uidnext
                self._consider(state, target, cursor.folder, search_address(transport, target))
                continue
            if status.uidnext <= cursor.next_uid:
                continue
            new_uids = search_address(transport, target, cursor.next_uid)
            cursor.next_uid = status.uidnext
            self._consider(state, target, cursor.folder, new_uids)

    def _consider(self, state: WaitState, target: WaitTarget, folder: str, uids: list[int]) -> None:
        """Fetch the candidate UIDs whole, in one command, and record those matching address, marker and CTA."""
        candidates = [uid for uid in uids if (folder, uid) not in state.matches]
        if not candidates:
            return
        for uid, raw in sorted(self._connection().fetch_messages(candidates).items()):
            accept(state, target, self._extractor, (folder, uid), parse_email(raw))


@dataclass
class _Rule:
    """When a wait is done: its deadline, its count test and its settle window."""

    deadline: float
    satisfied: Callable[[list[ReceivedEmail]], bool]
    settle: float
    settle_until: float | None = None

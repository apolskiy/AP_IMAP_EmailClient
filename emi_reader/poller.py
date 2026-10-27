# SPDX-FileCopyrightText: 2026 Aleksandr Polskiy
# SPDX-License-Identifier: Apache-2.0
"""One poll loop for every waiting request of an account (design §5.9, AMB-80).

Each request waiting on its own sends its own ``EXAMINE``, searches and fetch every poll. On Greenmail
every ``FETCH`` costs about 6 s at 15,600 messages, however little it asks for, and fetches run one after
another, so the waits slowed each other down. The poller sends one set of commands per cycle for all of
them: ``EXAMINE`` per folder, the pre-arrival search for new requests, one search over the new UIDs for
all waiting addresses (``OR``), and **one fetch** of every candidate; each request then matches the
parsed emails by its own rules (:mod:`emi_reader.matching`).

The poller only finds emails. Deadlines, counts, settle windows and the give-up decision stay with each
request (:class:`emi_reader.mailbox.Mailbox`). Network errors are retried with the reader's backoff,
the restart rule included (AMB-78); a fatal one ends every request waiting at that moment.
"""

import logging
import random
import threading
import time
from collections.abc import Callable
from contextlib import AbstractContextManager, ExitStack
from dataclasses import dataclass, field
from typing import Any

from emi_reader.accessibility import FolderAccess, probe_account
from emi_reader.errors import ImapConnectionError, LinkExtractionError, MailboxAccessError
from emi_reader.keywords import ReaderKeyword
from emi_reader.links import LinkExtractor
from emi_reader.matching import (
    SEARCH_KEYS,
    Cursor,
    WaitState,
    WaitTarget,
    accept,
    addressed_to,
    check_expected_folder,
    readable_folders,
    search_address,
)
from emi_reader.mime import ParsedEmail, parse_email
from emi_reader.settings import ImapSettings
from emi_reader.transport import FolderStatus, ImaplibTransport, ImapTransport

_LOGGER = logging.getLogger(__name__)
_MAX_TERMS = 20  # (address, field) terms per OR search, so one command stays short


@dataclass(eq=False)
class PolledWait:  # pylint: disable=too-many-instance-attributes
    """One request served by the poller; the poller fills it, the waiting thread reads it after each cycle.

    Attributes:
        target: What the request looks for.
        extractor: Its link extraction (mismatch rule).
        state: What it found; None until its first cycle.
        access: Folder accessibility at its first cycle.
        attempts: Network failures retried while it waited.
        last_error: The latest cycle's retried failure, or None after a good cycle.
        error: A fatal error that ends the request.
        cycles: Cycles finished since it registered.
    """

    target: WaitTarget
    extractor: LinkExtractor
    state: WaitState | None = None
    access: list[FolderAccess] = field(default_factory=list)
    attempts: list[dict[str, Any]] = field(default_factory=list)
    last_error: ImapConnectionError | None = None
    error: Exception | None = None
    cycles: int = 0


class SharedPoller:  # pylint: disable=too-many-instance-attributes
    """Polls an account on one connection for every registered request (a background thread)."""

    def __init__(  # pylint: disable=too-many-arguments
        self,
        settings: ImapSettings,
        *,
        poll_interval: float = 2.0,
        poll_jitter: float = 0.5,
        backoff: tuple[float, ...] = (0.5, 1.0, 2.0, 4.0, 5.0),
        transport_factory: Callable[[ImapSettings], ImapTransport] = ImaplibTransport,
        guard: Callable[[], AbstractContextManager[Any]] | None = None,
        idle_close_sec: float = 60.0,
        rng: random.Random | None = None,
    ) -> None:
        """Create the poller; its thread starts with the first request.

        Args:
            settings: Connection settings.
            poll_interval: Seconds between cycles while requests wait.
            poll_jitter: Random seconds added to each interval (0..jitter).
            backoff: Delays after failed cycles; the last value repeats.
            transport_factory: Builds the connection (a simulated one in unit tests).
            guard: Held while connected, e.g. a slot of the broker's connection budget; ``TimeoutError``
                from it is a retried ``READER_IMAP_CONNECTION_LIMIT``.
            idle_close_sec: Close the connection (and end the thread) after this long with no request.
            rng: Random source for the jitter.
        """
        self._settings = settings
        self._interval = poll_interval
        self._jitter = poll_jitter
        self._backoff = backoff or (poll_interval,)
        self._factory = transport_factory
        self._guard = guard
        self._idle_close = idle_close_sec
        self._rng = rng or random.Random()
        self._lock = threading.Lock()
        self._wake = threading.Condition(self._lock)
        self._done = threading.Condition(self._lock)
        self._io = threading.Lock()  # one IMAP command at a time on the connection
        self._requests: list[PolledWait] = []
        self._thread: threading.Thread | None = None
        self._closing = False
        self._transport: ImapTransport | None = None
        self._held: ExitStack | None = None
        self._access: list[FolderAccess] = []
        self._cursors: dict[str, Cursor] = {}
        self._had_session = False
        self._failures = 0
        self._cycles = 0

    @property
    def settings(self) -> ImapSettings:
        """The account's connection settings."""
        return self._settings

    @property
    def cycles(self) -> int:
        """Cycles run so far (each one ``EXAMINE`` per folder and at most one fetch per folder)."""
        return self._cycles

    def register(self, target: WaitTarget, extractor: LinkExtractor) -> PolledWait:
        """Add a request; it is served from the next cycle on, which starts at once.

        Args:
            target: What it looks for.
            extractor: Its link extraction.

        Returns:
            The request, to pass to :meth:`next_cycle` and :meth:`unregister`.

        Raises:
            RuntimeError: After :meth:`close`.
        """
        request = PolledWait(target, extractor)
        with self._lock:
            if self._closing:
                raise RuntimeError("the poller is closed")
            self._requests.append(request)
            if self._thread is None:
                self._thread = threading.Thread(target=self._run, name="emi-shared-poller", daemon=True)
                self._thread.start()
            self._wake.notify_all()
        return request

    def unregister(self, request: PolledWait) -> None:
        """Remove a request (safe to call twice).

        Args:
            request: A registered request.
        """
        with self._lock:
            if request in self._requests:
                self._requests.remove(request)

    def next_cycle(self, request: PolledWait, timeout: float) -> None:
        """Block until a cycle finished for the request, or ``timeout`` seconds passed.

        Args:
            request: A registered request.
            timeout: Most seconds to wait.

        Raises:
            Exception: The request's fatal error (network, access or link extraction).
        """
        deadline = time.monotonic() + max(timeout, 0.0)
        with self._lock:
            seen = request.cycles
            while request.cycles == seen and request.error is None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._done.wait(remaining)
            if request.error is not None:
                raise request.error

    def probe(self) -> list[FolderAccess]:
        """Probe the folders again on the poller's connection (the filter check at give-up, design §5.3).

        Returns:
            Accessibility of each configured folder.
        """
        with self._io:
            transport = self._connection()
            return probe_account(transport, self._settings.username, self._settings.folders)

    def close(self) -> None:
        """Stop the thread and close the connection; requests still waiting end at their next cycle wait."""
        with self._lock:
            self._closing = True
            thread = self._thread
            self._wake.notify_all()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=10)
        with self._io:
            self._disconnect()

    # --- the loop --------------------------------------------------------------------------------------

    def _run(self) -> None:
        """Cycle while requests wait; close the connection and end after ``idle_close_sec`` without one."""
        while True:
            with self._lock:
                idle_until = time.monotonic() + self._idle_close
                while not self._requests and not self._closing:
                    remaining = idle_until - time.monotonic()
                    if remaining <= 0:
                        self._thread = None
                        break
                    self._wake.wait(remaining)
                if self._closing or self._thread is None:
                    break
                requests = list(self._requests)
            delay = self._cycle(requests)
            with self._lock:
                self._cycles += 1
                for request in requests:
                    request.cycles += 1
                self._done.notify_all()
                if self._requests and not self._closing:
                    self._wake.wait(delay)  # a new request wakes the poller early
        with self._io:
            self._disconnect()

    def _cycle(self, requests: list[PolledWait]) -> float:
        """One look for every request; the delay before the next one."""
        with self._io:
            try:
                self._look(requests)
            except ImapConnectionError as error:
                if not (error.retryable or self._restarting(error)):
                    self._fail(requests, error)
                    return self._interval
                self._had_session = self._had_session or error.retryable  # only a live session drops or times out
                self._failures += 1
                _LOGGER.warning("shared poller retrying after %s: %s", error.primary, error)
                for request in requests:
                    request.attempts.append({"keyword": error.primary, "message": str(error), "at": time.monotonic()})
                    request.last_error = error
                self._disconnect()
                return self._backoff[min(self._failures - 1, len(self._backoff) - 1)]
            except MailboxAccessError as error:
                self._fail(requests, error)
                return self._interval
        self._failures, self._had_session = 0, True
        for request in requests:
            request.last_error = None
        return self._interval + self._rng.uniform(0.0, self._jitter)

    def _restarting(self, error: ImapConnectionError) -> bool:
        """A refused reconnect after the poller had a working connection: most likely a server restart,
        retried like a dropped connection (AMB-78). A refused first connect stays fatal."""
        return self._had_session and error.primary == ReaderKeyword.IMAP_CONNECTION_REFUSED

    def _fail(self, requests: list[PolledWait], error: Exception) -> None:
        """End every request of the cycle with a fatal error and drop the connection."""
        for request in requests:
            request.error = error
        self._disconnect()

    def _connection(self) -> ImapTransport:
        """The open connection; a new one is probed for folder access (caller holds ``_io``)."""
        if self._transport is None:
            stack = ExitStack()
            try:
                if self._guard is not None:
                    stack.enter_context(self._guard())
                transport = self._factory(self._settings)
                transport.open()
                self._access = probe_account(transport, self._settings.username, self._settings.folders,
                                             raise_retryable=True)
            except TimeoutError as error:
                stack.close()
                raise ImapConnectionError("no connection slot became free for the shared poller",
                                          ReaderKeyword.IMAP_CONNECTION_LIMIT, retryable=True,
                                          details={"source": "broker budget"}) from error
            except BaseException:
                stack.close()
                raise
            self._transport, self._held = transport, stack
        self._transport.open()
        return self._transport

    def _disconnect(self) -> None:
        """Close the connection and give its slot back (caller holds ``_io``)."""
        transport, held = self._transport, self._held
        self._transport, self._held = None, None
        if transport is not None:
            transport.close()
        if held is not None:
            held.close()

    # --- one look --------------------------------------------------------------------------------------

    def _look(self, requests: list[PolledWait]) -> None:
        """``EXAMINE`` each readable folder, search, fetch once per folder and hand the emails out."""
        transport = self._connection()
        readable = readable_folders(self._access, self._settings)
        for request in requests:
            if request.state is None and request.error is None:
                request.access = list(self._access)
                try:
                    check_expected_folder(self._access, request.target, self._settings)
                except MailboxAccessError as error:  # ends this request only (AMB-85)
                    request.error = error
                    continue
                request.state = WaitState(address=request.target.address, access_start=list(self._access))
        fresh = [request for request in requests if request.state is not None and request.state.before is None]
        for entry in readable:
            status = transport.examine(entry.folder)
            self._folder(transport, entry.folder, status, requests, fresh)
        for request in fresh:
            if request.state is not None:
                request.state.before = request.state.snapshot()

    def _folder(self, transport: ImapTransport, folder: str, status: FolderStatus, requests: list[PolledWait],
                fresh: list[PolledWait]) -> None:
        """Search one folder for every request and fetch its candidates in one command."""
        cursor = self._cursors.get(folder)
        whole = list(fresh)
        if cursor is not None and cursor.uidvalidity != status.uidvalidity:
            _LOGGER.info("%s: %s %d -> %d; searching again", ReaderKeyword.IMAP_UIDVALIDITY_CHANGED, folder,
                         cursor.uidvalidity, status.uidvalidity)
            for request in requests:
                if request.state is not None:
                    request.state.forget_folder(folder)
            whole = list(requests)
        candidates: set[int] = set()
        for request in whole:
            candidates.update(search_address(transport, request.target))
        waiting = [request for request in requests if request not in whole]
        if waiting and cursor is not None and status.uidnext > cursor.next_uid:
            candidates.update(_search_new(transport, [request.target for request in waiting], cursor.next_uid))
        self._cursors[folder] = Cursor(folder, status.uidvalidity, status.uidnext)
        for request in requests:
            if request.state is not None:
                request.state.record_status(folder, status)
        if candidates:
            emails = {uid: parse_email(raw) for uid, raw in transport.fetch_messages(sorted(candidates)).items()}
            for request in requests:
                _hand_out(request, folder, emails)


def _search_new(transport: ImapTransport, targets: list[WaitTarget], since_uid: int) -> set[int]:
    """New UIDs at any waiting address on any of its fields: ``OR`` searches of at most ``_MAX_TERMS`` terms."""
    terms = list(dict.fromkeys((target.address, name) for target in targets for name in target.match_fields))
    found: set[int] = set()
    for start in range(0, len(terms), _MAX_TERMS):
        chunk = terms[start:start + _MAX_TERMS]
        criteria: list[str] = ["UID", f"{since_uid}:*"]
        for position, (address, name) in enumerate(chunk):
            if position < len(chunk) - 1:
                criteria.append("OR")
            criteria.extend((*SEARCH_KEYS[name], f'"{address}"'))
        found.update(uid for uid in transport.uid_search(tuple(criteria)) if uid >= since_uid)
    return found


def _hand_out(request: PolledWait, folder: str, emails: dict[int, ParsedEmail]) -> None:
    """Give a request the fetched emails at its address; a broken link ends that request only."""
    state = request.state
    if state is None or request.error is not None:
        return
    for uid, email in sorted(emails.items()):
        if (folder, uid) in state.matches or not addressed_to(email, request.target):
            continue
        try:
            accept(state, request.target, request.extractor, (folder, uid), email)
        except LinkExtractionError as error:
            request.error = error
            return

# SPDX-FileCopyrightText: 2026 Aleksandr Polskiy
# SPDX-License-Identifier: Apache-2.0
"""Named faults: deliberately broken tools for proving that tests can fail (design §13.4, AMB-64).

A fault wraps the API's backend (:class:`emi_api.MailBackend`) and breaks one behaviour; every other
call, including the run-lease calls of service mode, reaches the real backend unchanged. The tools' own
code carries no fault flags. ``--emi-fault <name>`` (``EMI_FAULT``) selects one for a run. Which tests
must catch each fault, and with which keywords, is listed with the suite (``tests/functional/validity.py``).
"""

import re
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any

from emi_api import MailBackend
from emi_harness.session import PROBE_CASE
from emi_reader import Cta, ReceivedEmail
from emi_sender import BatchRequest, Field, Manifest, Marker

NO_FAULT = "none"
_ALL_FIELDS = ("to", "cc", "bcc", "delivered-to", "x-original-to")
_SLOT = re.compile(r"-r\d{2}@")
_GIVE_UP_SEC = 1.0


class FaultyBackend:
    """Passes every call to the real backend; a fault overrides the calls it breaks."""

    def __init__(self, inner: MailBackend) -> None:
        """Wrap a backend.

        Args:
            inner: The real backend.
        """
        self._inner = inner

    @property
    def inner(self) -> MailBackend:
        """The real backend."""
        return self._inner

    def __getattr__(self, name: str) -> Any:
        """Everything not overridden: the real backend's attribute (health, leases, close)."""
        return getattr(self._inner, name)

    def send(self, kind: str, address: str, count: int = 1, *, marker: Marker | None = None) -> Manifest:
        """Send ``count`` emails of one kind to one address.

        Args:
            kind: ``activation``, ``invitation`` or ``generic``.
            address: Recipient.
            count: Number of emails.
            marker: Test/run marker.

        Returns:
            The manifest.
        """
        return self._inner.send(kind, address, count, marker=marker)

    def send_batch(self, request: BatchRequest, *, marker: Marker | None = None) -> Manifest:
        """Send a batch.

        Args:
            request: The batch.
            marker: Test/run marker.

        Returns:
            The manifest.
        """
        return self._inner.send_batch(request, marker=marker)

    def wait_for_link(  # pylint: disable=too-many-arguments
        self, address: str, cta: Cta, *, timeout: float = 60.0, test_id: str | None = None,
        run_id: str | None = None, match_fields: tuple[str, ...] | None = None,
    ) -> str:
        """Wait for an email with the given button and return its link.

        Args:
            address: The test's own address.
            cta: The button.
            timeout: Seconds to wait.
            test_id: Required test marker.
            run_id: Required run marker.
            match_fields: Recipient fields the address must appear in.

        Returns:
            The CTA link.
        """
        return self._inner.wait_for_link(address, cta, timeout=timeout, test_id=test_id, run_id=run_id,
                                         match_fields=match_fields)

    def wait_for_emails(  # pylint: disable=too-many-arguments
        self, address: str, *, cta: Cta | None = None, count: int = 1, timeout: float = 60.0,
        test_id: str | None = None, run_id: str | None = None, match_fields: tuple[str, ...] | None = None,
        settle_window: float | None = None,
    ) -> list[ReceivedEmail]:
        """Wait until exactly ``count`` matching emails are present.

        Args:
            address: The test's own address.
            cta: Only emails with this button count.
            count: Number expected.
            timeout: Seconds to wait.
            test_id: Required test marker.
            run_id: Required run marker.
            match_fields: Recipient fields the address must appear in.
            settle_window: Seconds to watch for extra emails.

        Returns:
            The emails, oldest first.
        """
        return self._inner.wait_for_emails(address, cta=cta, count=count, timeout=timeout, test_id=test_id,
                                           run_id=run_id, match_fields=match_fields, settle_window=settle_window)


class SenderDropsEmail(FaultyBackend):
    """``send`` delivers one email fewer than asked; the manifest still lists them all.

    The missing email goes to a sink address no test waits for, so its manifest entry is a real one.
    """

    def send(self, kind: str, address: str, count: int = 1, *, marker: Marker | None = None) -> Manifest:
        """Send ``count - 1`` emails to the address and one to the sink; report all of them as the address's.

        Args:
            kind: ``activation``, ``invitation`` or ``generic``.
            address: Recipient.
            count: Number of emails.
            marker: Test/run marker.

        Returns:
            A manifest listing ``count`` emails to the address.
        """
        dropped = self.inner.send(kind, _sink(address), 1, marker=marker)
        delivered = self.inner.send(kind, address, count - 1, marker=marker) if count > 1 else None
        return _merged(delivered, dropped, address)

    def send_batch(self, request: BatchRequest, *, marker: Marker | None = None) -> Manifest:
        """Send one email of the first kind to the sink and the rest as asked; report all of them.

        A request with an explicit per-address plan, or without a positive integer count, is sent unchanged.

        Args:
            request: The batch.
            marker: Test/run marker.

        Returns:
            A manifest listing every asked email, the dropped one under the first address.
        """
        positive = [(name, value) for name, value in request.counts.items() if isinstance(value, int) and value > 0]
        if not positive or request.explicit:
            return self.inner.send_batch(request, marker=marker)
        kind, count = positive[0]
        address = request.addresses[0]
        dropped = self.inner.send(kind, _sink(address), 1, marker=marker)
        rest = {name: value for name, value in {**request.counts, kind: count - 1}.items() if value}
        delivered = self.inner.send_batch(replace(request, counts=rest), marker=marker) if rest else None
        return _merged(delivered, dropped, address)


class SenderIgnoresField(FaultyBackend):
    """A batch puts the address in ``To``, whatever recipient field was asked."""

    def send_batch(self, request: BatchRequest, *, marker: Marker | None = None) -> Manifest:
        """Send the batch with the address in ``To``.

        Args:
            request: The batch.
            marker: Test/run marker.

        Returns:
            The manifest.
        """
        return self.inner.send_batch(replace(request, recipient_field=Field.TO), marker=marker)


class ReaderAnyField(FaultyBackend):
    """A wait matches the address in every recipient field, whatever ``match_fields`` says."""

    def wait_for_emails(  # pylint: disable=too-many-arguments
        self, address: str, *, cta: Cta | None = None, count: int = 1, timeout: float = 60.0,
        test_id: str | None = None, run_id: str | None = None, match_fields: tuple[str, ...] | None = None,
        settle_window: float | None = None,
    ) -> list[ReceivedEmail]:
        """Wait with every recipient field.

        Args:
            address: The test's own address.
            cta: Only emails with this button count.
            count: Number expected.
            timeout: Seconds to wait.
            test_id: Required test marker.
            run_id: Required run marker.
            match_fields: Ignored.
            settle_window: Seconds to watch for extra emails.

        Returns:
            The emails, oldest first.
        """
        return self.inner.wait_for_emails(address, cta=cta, count=count, timeout=timeout, test_id=test_id,
                                          run_id=run_id, match_fields=_ALL_FIELDS, settle_window=settle_window)


class ReaderIgnoresMarkers(FaultyBackend):
    """A wait drops its test and run markers."""

    def wait_for_emails(  # pylint: disable=too-many-arguments
        self, address: str, *, cta: Cta | None = None, count: int = 1, timeout: float = 60.0,
        test_id: str | None = None, run_id: str | None = None, match_fields: tuple[str, ...] | None = None,
        settle_window: float | None = None,
    ) -> list[ReceivedEmail]:
        """Wait without markers.

        Args:
            address: The test's own address.
            cta: Only emails with this button count.
            count: Number expected.
            timeout: Seconds to wait.
            test_id: Ignored.
            run_id: Ignored.
            match_fields: Recipient fields the address must appear in.
            settle_window: Seconds to watch for extra emails.

        Returns:
            The emails, oldest first.
        """
        return self.inner.wait_for_emails(address, cta=cta, count=count, timeout=timeout, test_id=None, run_id=None,
                                          match_fields=match_fields, settle_window=settle_window)


class ReaderSlotCollapse(FaultyBackend):
    """A wait for a slot address ``<run>-rNN`` reads the test's slot 0 address instead."""

    def wait_for_emails(  # pylint: disable=too-many-arguments
        self, address: str, *, cta: Cta | None = None, count: int = 1, timeout: float = 60.0,
        test_id: str | None = None, run_id: str | None = None, match_fields: tuple[str, ...] | None = None,
        settle_window: float | None = None,
    ) -> list[ReceivedEmail]:
        """Wait on the address with its slot removed.

        Args:
            address: The test's own address.
            cta: Only emails with this button count.
            count: Number expected.
            timeout: Seconds to wait.
            test_id: Required test marker.
            run_id: Required run marker.
            match_fields: Recipient fields the address must appear in.
            settle_window: Seconds to watch for extra emails.

        Returns:
            The emails, oldest first.
        """
        return self.inner.wait_for_emails(_SLOT.sub("@", address), cta=cta, count=count, timeout=timeout,
                                          test_id=test_id, run_id=run_id, match_fields=match_fields,
                                          settle_window=settle_window)


class _ResultFault(FaultyBackend):
    """A fault that changes what a correct wait returned."""

    def wait_for_emails(  # pylint: disable=too-many-arguments
        self, address: str, *, cta: Cta | None = None, count: int = 1, timeout: float = 60.0,
        test_id: str | None = None, run_id: str | None = None, match_fields: tuple[str, ...] | None = None,
        settle_window: float | None = None,
    ) -> list[ReceivedEmail]:
        """The real wait, then :meth:`_change`.

        Args:
            address: The test's own address.
            cta: Only emails with this button count.
            count: Number expected.
            timeout: Seconds to wait.
            test_id: Required test marker.
            run_id: Required run marker.
            match_fields: Recipient fields the address must appear in.
            settle_window: Seconds to watch for extra emails.

        Returns:
            The changed emails.
        """
        return self._change(self.inner.wait_for_emails(address, cta=cta, count=count, timeout=timeout,
                                                       test_id=test_id, run_id=run_id, match_fields=match_fields,
                                                       settle_window=settle_window))

    def _change(self, emails: list[ReceivedEmail]) -> list[ReceivedEmail]:
        """The fault's change; none by default."""
        return emails


class ReaderDuplicatesEmail(_ResultFault):
    """A wait returns one email twice in place of another (the count stays right)."""

    def _change(self, emails: list[ReceivedEmail]) -> list[ReceivedEmail]:
        """The first email in place of the last."""
        return [*emails[:-1], emails[0]] if len(emails) > 1 else emails


class ReaderReturnsExtra(_ResultFault):
    """A wait returns one email more than asked."""

    def _change(self, emails: list[ReceivedEmail]) -> list[ReceivedEmail]:
        """The first email once more."""
        return [*emails, emails[0]] if emails else emails


class ReaderWrongLink(_ResultFault):
    """The link returned differs from the email's link in its last character."""

    def wait_for_link(  # pylint: disable=too-many-arguments
        self, address: str, cta: Cta, *, timeout: float = 60.0, test_id: str | None = None,
        run_id: str | None = None, match_fields: tuple[str, ...] | None = None,
    ) -> str:
        """The real link, changed.

        Args:
            address: The test's own address.
            cta: The button.
            timeout: Seconds to wait.
            test_id: Required test marker.
            run_id: Required run marker.
            match_fields: Recipient fields the address must appear in.

        Returns:
            A wrong link.
        """
        return _wrong(self.inner.wait_for_link(address, cta, timeout=timeout, test_id=test_id, run_id=run_id,
                                               match_fields=match_fields))

    def _change(self, emails: list[ReceivedEmail]) -> list[ReceivedEmail]:
        """Every link changed."""
        return [replace(item, link=_wrong(item.link)) if item.link else item for item in emails]


class ReaderGivesUpEarly(FaultyBackend):
    """A wait gives up after 1 s, whatever its timeout."""

    def wait_for_link(  # pylint: disable=too-many-arguments
        self, address: str, cta: Cta, *, timeout: float = 60.0, test_id: str | None = None,
        run_id: str | None = None, match_fields: tuple[str, ...] | None = None,
    ) -> str:
        """Wait at most 1 s.

        Args:
            address: The test's own address.
            cta: The button.
            timeout: Capped at 1 s.
            test_id: Required test marker.
            run_id: Required run marker.
            match_fields: Recipient fields the address must appear in.

        Returns:
            The CTA link.
        """
        return self.inner.wait_for_link(address, cta, timeout=min(timeout, _GIVE_UP_SEC), test_id=test_id,
                                        run_id=run_id, match_fields=match_fields)

    def wait_for_emails(  # pylint: disable=too-many-arguments
        self, address: str, *, cta: Cta | None = None, count: int = 1, timeout: float = 60.0,
        test_id: str | None = None, run_id: str | None = None, match_fields: tuple[str, ...] | None = None,
        settle_window: float | None = None,
    ) -> list[ReceivedEmail]:
        """Wait at most 1 s.

        Args:
            address: The test's own address.
            cta: Only emails with this button count.
            count: Number expected.
            timeout: Capped at 1 s.
            test_id: Required test marker.
            run_id: Required run marker.
            match_fields: Recipient fields the address must appear in.
            settle_window: Seconds to watch for extra emails.

        Returns:
            The emails, oldest first.
        """
        return self.inner.wait_for_emails(address, cta=cta, count=count, timeout=min(timeout, _GIVE_UP_SEC),
                                          test_id=test_id, run_id=run_id, match_fields=match_fields,
                                          settle_window=settle_window)


def _sink(address: str) -> str:
    """The address a dropped email goes to: next to the test's own, and waited on by no test."""
    local, domain = address.rsplit("@", 1)
    return f"{local}-dropped@{domain}"


def _merged(delivered: Manifest | None, dropped: Manifest, address: str) -> Manifest:
    """The delivered manifest with the dropped email appended as if it had gone to ``address``."""
    entries = list(delivered.entries) if delivered else []
    entries.extend(replace(entry, index=len(entries), address=address) for entry in dropped.entries)
    return replace(delivered or dropped, entries=entries)


def _wrong(link: str) -> str:
    """The link with its last character changed."""
    return link[:-1] + ("1" if link.endswith("0") else "0")


@dataclass(frozen=True)
class Fault:
    """One named fault.

    Attributes:
        name: Its name, as given to ``--emi-fault``.
        breaks: What it breaks, in one sentence.
        wrap: Builds the faulty backend around the real one.
    """

    name: str
    breaks: str
    wrap: Callable[[MailBackend], FaultyBackend]


FAULTS: dict[str, Fault] = {fault.name: fault for fault in (
    Fault("sender-drops-email", "send delivers one email fewer than asked; the manifest lists them all",
          SenderDropsEmail),
    Fault("sender-ignores-field", "a batch puts the address in To, whatever field was asked", SenderIgnoresField),
    Fault("reader-any-field", "a wait matches the address in every recipient field", ReaderAnyField),
    Fault("reader-ignores-markers", "a wait drops its test and run markers", ReaderIgnoresMarkers),
    Fault("reader-slot-collapse", "a wait for a slot address reads slot 0 instead", ReaderSlotCollapse),
    Fault("reader-duplicates-email", "a wait returns one email twice in place of another", ReaderDuplicatesEmail),
    Fault("reader-returns-extra", "a wait returns one email more than asked", ReaderReturnsExtra),
    Fault("reader-wrong-link", "the link returned differs in its last character", ReaderWrongLink),
    Fault("reader-gives-up-early", "a wait gives up after 1 s, whatever its timeout", ReaderGivesUpEarly),
)}
FAULT_CHOICES: tuple[str, ...] = (NO_FAULT, *FAULTS)


def apply_fault(name: str, backend: MailBackend) -> MailBackend:
    """The backend with the named fault, or unchanged for ``none``.

    Args:
        name: A fault name, or ``none``.
        backend: The real backend.

    Returns:
        The backend to give the API.

    Raises:
        ValueError: For an unknown name.
    """
    if name == NO_FAULT:
        return backend
    if name not in FAULTS:
        raise ValueError(f"unknown fault {name!r}; known: {', '.join(FAULT_CHOICES)}")
    return _ProbeExempt(backend, FAULTS[name].wrap(backend))


def _is_probe(address: str) -> bool:
    """Whether the address is the session probe's (``qa+C000000_<run>``)."""
    return f"+{PROBE_CASE}_".lower() in address.lower()


class _ProbeExempt(FaultyBackend):
    """The fault for the tests' calls, the real backend for the session probe's.

    A fault breaks what the tests check, never the setup check before them: a probe that a fault breaks would
    skip the whole run as an infrastructure failure, and no test would get the chance to catch the fault.
    """

    def __init__(self, inner: MailBackend, faulty: FaultyBackend) -> None:
        """Route between the real backend and the faulty one.

        Args:
            inner: The real backend.
            faulty: The fault around it.
        """
        super().__init__(inner)
        self._faulty = faulty

    def send(self, kind: str, address: str, count: int = 1, *, marker: Marker | None = None) -> Manifest:
        """Send through the fault unless it is the probe."""
        target = self.inner if _is_probe(address) else self._faulty
        return target.send(kind, address, count, marker=marker)

    def send_batch(self, request: BatchRequest, *, marker: Marker | None = None) -> Manifest:
        """Send through the fault unless it is the probe."""
        target = self.inner if any(_is_probe(address) for address in request.addresses) else self._faulty
        return target.send_batch(request, marker=marker)

    def wait_for_link(  # pylint: disable=too-many-arguments
        self, address: str, cta: Cta, *, timeout: float = 60.0, test_id: str | None = None,
        run_id: str | None = None, match_fields: tuple[str, ...] | None = None,
    ) -> str:
        """Wait through the fault unless it is the probe."""
        target = self.inner if _is_probe(address) else self._faulty
        return target.wait_for_link(address, cta, timeout=timeout, test_id=test_id, run_id=run_id,
                                    match_fields=match_fields)

    def wait_for_emails(  # pylint: disable=too-many-arguments
        self, address: str, *, cta: Cta | None = None, count: int = 1, timeout: float = 60.0,
        test_id: str | None = None, run_id: str | None = None, match_fields: tuple[str, ...] | None = None,
        settle_window: float | None = None,
    ) -> list[ReceivedEmail]:
        """Wait through the fault unless it is the probe."""
        target = self.inner if _is_probe(address) else self._faulty
        return target.wait_for_emails(address, cta=cta, count=count, timeout=timeout, test_id=test_id,
                                      run_id=run_id, match_fields=match_fields, settle_window=settle_window)

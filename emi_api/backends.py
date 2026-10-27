# SPDX-FileCopyrightText: 2026 Aleksandr Polskiy
# SPDX-License-Identifier: Apache-2.0
"""The two ways the API reaches the sender and reader (design §2).

``service`` mode uses :class:`emi_test_broker.BrokerClient` (one broker per host shares connections and the
log). ``inprocess`` mode runs the same :class:`emi_test_broker.BrokerService` request code inside the test
process, so both modes apply the same connection budget, pooling and errors; only HTTP is skipped.
"""

import getpass
import os
import socket
from typing import Any, Protocol

from emi_reader import Cta, ReceivedEmail
from emi_sender import BatchRequest, Manifest, Marker
from emi_test_broker import BrokerService
from emi_test_broker.codec import batch_request_to_json, cta_to_json, manifest_from_json, received_from_json


class MailBackend(Protocol):
    """What the API facades need from either mode."""

    def send(self, kind: str, address: str, count: int = 1, *, marker: Marker | None = None) -> Manifest:
        """Send ``count`` emails of one kind to one address."""

    def send_batch(self, request: BatchRequest, *, marker: Marker | None = None) -> Manifest:
        """Send a batch."""

    def wait_for_link(  # pylint: disable=too-many-arguments
        self, address: str, cta: Cta, *, timeout: float = 60.0, test_id: str | None = None,
        run_id: str | None = None, match_fields: tuple[str, ...] | None = None,
    ) -> str:
        """Wait for an email with the given button and return its link."""

    def wait_for_emails(  # pylint: disable=too-many-arguments
        self, address: str, *, cta: Cta | None = None, count: int = 1, timeout: float = 60.0,
        test_id: str | None = None, run_id: str | None = None, match_fields: tuple[str, ...] | None = None,
        settle_window: float | None = None,
    ) -> list[ReceivedEmail]:
        """Wait until exactly ``count`` matching emails are present."""


class LocalBroker:
    """``inprocess`` mode: the broker's request code called directly, without HTTP or leases."""

    def __init__(self, service: BrokerService) -> None:
        """Wrap a service.

        Args:
            service: The in-process service (owns the connection pool).
        """
        self._service = service

    @property
    def service(self) -> BrokerService:
        """The wrapped service."""
        return self._service

    def health(self) -> dict[str, Any]:
        """The service's health document.

        Returns:
            Identity, pool usage and settings.
        """
        return self._service.health()

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
        payload = {"kind": kind, "address": address, "count": count, "marker": _marker(marker)}
        return manifest_from_json(self._service.send(payload))

    def send_batch(self, request: BatchRequest, *, marker: Marker | None = None) -> Manifest:
        """Send a batch.

        Args:
            request: The batch.
            marker: Test/run marker.

        Returns:
            The manifest.
        """
        payload = {"batch": batch_request_to_json(request), "marker": _marker(marker)}
        return manifest_from_json(self._service.send(payload))

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
            match_fields: Recipient fields the address must appear in; ``To`` only by default.

        Returns:
            The CTA link.
        """
        payload = {"address": address, "cta": cta_to_json(cta), "mode": "link", "timeout": timeout,
                   "test_id": test_id, "run_id": run_id, "match_fields": _fields(match_fields)}
        return str(self._service.wait(payload)["link"])

    def wait_for_emails(  # pylint: disable=too-many-arguments
        self, address: str, *, cta: Cta | None = None, count: int = 1, timeout: float = 60.0,
        test_id: str | None = None, run_id: str | None = None, match_fields: tuple[str, ...] | None = None,
        settle_window: float | None = None,
    ) -> list[ReceivedEmail]:
        """Wait until exactly ``count`` matching emails are present.

        Args:
            address: The test's own address.
            cta: Only emails with this button count; None accepts any.
            count: Number expected.
            timeout: Seconds to wait.
            test_id: Required test marker.
            run_id: Required run marker.
            match_fields: Recipient fields the address must appear in; ``To`` only by default.
            settle_window: Seconds to watch for extra emails after ``count`` arrived; the reader's default
                (2 s) when None; 0 for mail delivered before the wait.

        Returns:
            The emails, oldest first.
        """
        payload = {"address": address, "cta": cta_to_json(cta), "mode": "emails", "count": count,
                   "timeout": timeout, "test_id": test_id, "run_id": run_id,
                   "match_fields": _fields(match_fields), "settle_window": settle_window}
        return [received_from_json(item) for item in self._service.wait(payload)["emails"]]

    def close(self) -> None:
        """Close every pooled connection."""
        self._service.close()


def holder_name(mode: str) -> str:
    """Who holds a run lease: host, user, process and mode.

    Args:
        mode: Client mode.

    Returns:
        For example ``build-07/qa pid 4242 service``.
    """
    try:
        user = getpass.getuser()
    except OSError:
        user = "unknown"
    return f"{socket.gethostname()}/{user} pid {os.getpid()} {mode}"


def _marker(marker: Marker | None) -> dict[str, str]:
    """Marker as the service's JSON shape."""
    return {"test_id": marker.test_id, "run_id": marker.run_id} if marker else {}


def _fields(match_fields: tuple[str, ...] | None) -> list[str] | None:
    """Recipient fields as JSON (None keeps the reader's default)."""
    return list(match_fields) if match_fields is not None else None

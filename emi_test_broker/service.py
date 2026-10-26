# SPDX-FileCopyrightText: 2026 Aleksandr Polskiy
# SPDX-License-Identifier: Apache-2.0
"""What the broker does for each request, free of HTTP so unit tests call it directly (design §7).

The broker is a transport layer over the same library code: sends go to :class:`emi_sender.Sender`, waits to
:class:`emi_reader.Mailbox`. It owns every IMAP/SMTP connection on the host, so ``max_connections`` is a
real per-host budget across runs; requests queue for free slots.
"""

import getpass
import imaplib
import logging
import os
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
from datetime import date, datetime
from typing import Any

from emi_reader import ImapConnectionError, ImapSettings, Mailbox, ReaderKeyword
from emi_reader.transport import Phase, classify
from emi_sender import BatchRequest, Sender
from emi_test_broker.broker_log import LOGGER_NAME
from emi_test_broker.codec import (
    batch_request_from_json,
    cta_from_json,
    manifest_to_json,
    marker_from_json,
    received_to_json,
)
from emi_test_broker.errors import PurgeRefusedError
from emi_test_broker.keywords import TestBrokerKeyword
from emi_test_broker.leases import ConnectionBudget, LeaseTable
from emi_test_broker.purge import ImaplibPurgeTransport, PurgeRequest, PurgeTransportFactory, run_purge
from emi_test_broker.settings import BROKER_VERSION, BrokerSettings

_LOG = logging.getLogger(LOGGER_NAME)
SERVICE_NAME = "emi_test_broker"


def _require(payload: dict[str, Any], key: str) -> Any:
    """Return a required request field.

    Raises:
        ValueError: If it is missing.
    """
    if key not in payload or payload[key] is None:
        raise ValueError(f"request field '{key}' is required")
    return payload[key]


class BrokerService:  # pylint: disable=too-many-instance-attributes
    """Request handling shared by every run on the host."""

    def __init__(  # pylint: disable=too-many-arguments
        self,
        settings: BrokerSettings,
        *,
        mailbox_factory: Callable[[ImapSettings], Mailbox] = Mailbox,
        sender: Sender | None = None,
        purge_factory: PurgeTransportFactory = ImaplibPurgeTransport,
        clock: Callable[[], float] = time.monotonic,
        today: Callable[[], date] = date.today,
    ) -> None:
        """Create the service.

        Args:
            settings: Broker settings.
            mailbox_factory: Builds one reader mailbox (one IMAP connection) per pool slot.
            sender: The sender; defaults to one using ``settings.smtp``.
            purge_factory: Opens a read-write IMAP connection for purge.
            clock: Monotonic clock.
            today: Today's date (purge cut-off).
        """
        self._settings = settings
        self._mailbox_factory = mailbox_factory
        self._sender = sender or Sender(settings.smtp)
        self._purge_factory = purge_factory
        self._clock = clock
        self._today = today
        self._budget = ConnectionBudget(settings.max_connections)
        self._leases = LeaseTable(settings.lease_ttl_sec, clock)
        self._idle: list[Mailbox] = []
        self._idle_lock = threading.Lock()
        self._last_activity = clock()
        self._started = datetime.now().astimezone().isoformat(timespec="seconds")

    @property
    def settings(self) -> BrokerSettings:
        """The broker's settings."""
        return self._settings

    def touch(self) -> None:
        """Record activity (every request does)."""
        self._last_activity = self._clock()

    def idle_for(self) -> float:
        """Seconds since the last request."""
        return self._clock() - self._last_activity

    def has_active_runs(self) -> bool:
        """Whether any lease or wait is live."""
        return bool(self._leases.active())

    # --- endpoints -------------------------------------------------------------------------------------

    def health(self) -> dict[str, Any]:
        """``GET /health``: identity, pool usage and every setting in effect.

        Returns:
            The health document (passwords redacted).
        """
        return {"service": SERVICE_NAME, "ready": True, "version": BROKER_VERSION,
                "config_hash": self._settings.config_hash(), "pid": os.getpid(), "started": self._started,
                "pool": {"used": self._budget.used, "limit": self._budget.limit},
                "active_runs": sorted(self._leases.active()), "settings": self._settings.describe()}

    def send(self, payload: dict[str, Any]) -> dict[str, Any]:
        """``POST /send``: one kind to one address, or a whole batch.

        Args:
            payload: ``{"kind", "address", "count", "marker"}`` or ``{"batch": {...}, "marker"}``.

        Returns:
            The manifest.
        """
        marker = marker_from_json(payload.get("marker") or {})
        if payload.get("batch") is not None:
            request = batch_request_from_json(payload["batch"])
        else:
            request = BatchRequest(addresses=(str(_require(payload, "address")),),
                                   counts={str(_require(payload, "kind")): payload.get("count", 1)})
        with self._budget.slots(request.concurrency if isinstance(request.concurrency, int) else 1):
            manifest = self._sender.send_batch(request, marker=marker)
        primary, _secondary = manifest.keywords
        _LOG.info("send run=%s test=%s emails=%d sent=%d %s", marker.run_id or "-", marker.test_id or "-",
                  len(manifest), len(manifest.sent), primary or "ok")
        return manifest_to_json(manifest)

    def wait(self, payload: dict[str, Any]) -> dict[str, Any]:
        """``POST /wait``: long-poll for a test's email (time queued for a connection counts).

        Args:
            payload: ``{"address", "cta", "mode": "link"|"emails", "count", "timeout", "test_id", "run_id"}``.

        Returns:
            ``{"link": ...}`` or ``{"emails": [...]}``, plus the accessibility matrix and retried attempts.
        """
        address = str(_require(payload, "address"))
        timeout = float(payload.get("timeout", 60.0))
        run_id = payload.get("run_id") or None
        test_id = payload.get("test_id") or None
        cta = cta_from_json(payload.get("cta"))
        started = self._clock()
        result: dict[str, Any] = {}
        with self._leases.waiting(run_id or ""), self._slot(timeout):
            remaining = max(0.0, timeout - (self._clock() - started))
            mailbox = self._take_mailbox()
            healthy = False
            try:
                if payload.get("mode", "link") == "emails":
                    found = mailbox.wait_for_emails(address, cta=cta, count=int(payload.get("count", 1)),
                                                    timeout=remaining, test_id=test_id, run_id=run_id)
                    result = {"emails": [received_to_json(item) for item in found]}
                else:
                    if cta is None:
                        raise ValueError("request field 'cta' is required for mode 'link'")
                    result = {"link": mailbox.wait_for_link(address, cta, timeout=remaining, test_id=test_id,
                                                            run_id=run_id)}
                healthy = True
            finally:
                self._return_mailbox(mailbox, healthy)
        result.update({"access_matrix": mailbox.access_matrix, "attempts": mailbox.attempts,
                       "queued_sec": round(timeout - remaining, 3)})
        _LOG.info("wait run=%s test=%s address=%s ok", run_id or "-", test_id or "-", address)
        return result

    def lease(self, action: str, payload: dict[str, Any]) -> dict[str, Any]:
        """``POST /lease``, ``/lease/heartbeat``, ``/lease/release``.

        Args:
            action: ``acquire``, ``heartbeat`` or ``release``.
            payload: ``{"run_id", "holder"}``.

        Returns:
            The outcome.

        Raises:
            ValueError: If the action is unknown or the run ID missing.
        """
        run_id = str(_require(payload, "run_id"))
        if action == "acquire":
            self._leases.acquire(run_id, str(payload.get("holder", "")))
            _LOG.info("lease acquired run=%s holder=%s", run_id, payload.get("holder", ""))
            return {"run_id": run_id, "ttl_sec": self._settings.lease_ttl_sec}
        if action == "heartbeat":
            return {"run_id": run_id, "renewed": self._leases.heartbeat(run_id)}
        if action == "release":
            self._leases.release(run_id)
            _LOG.info("lease released run=%s", run_id)
            return {"run_id": run_id, "released": True}
        raise ValueError(f"unknown lease action '{action}'")

    def purge(self, payload: dict[str, Any]) -> dict[str, Any]:
        """``POST /purge``: refused while any run is active; no override.

        Args:
            payload: ``{"dry_run", "older_than_days", "everything", "requested_by"}``.

        Returns:
            Counts per folder.

        Raises:
            PurgeRefusedError: ``TEST_BROKER_PURGE_REFUSED`` naming the active run IDs.
            ImapConnectionError: If the mailbox cannot be opened or changed.
        """
        older = payload.get("older_than_days")
        request = PurgeRequest(dry_run=bool(payload.get("dry_run", False)),
                               older_than_days=int(older) if older is not None else None,
                               everything=bool(payload.get("everything", False)),
                               requested_by=str(payload.get("requested_by") or getpass.getuser()))
        if request.older_than_days is not None and request.older_than_days < 0:
            raise ValueError("older_than_days must not be negative")
        active = self._leases.active()
        if active:
            _LOG.warning("%s by %s: active runs %s", TestBrokerKeyword.PURGE_REFUSED, request.requested_by,
                         ", ".join(sorted(active)))
            raise PurgeRefusedError(f"Purge refused: {len(active)} run(s) active: {', '.join(sorted(active))}",
                                    TestBrokerKeyword.PURGE_REFUSED, details={"active_runs": sorted(active)})
        with self._budget.slots(1):
            report = run_purge(self._open_purge(), self._settings.imap.folders, request, self._today())
        _LOG.warning("purge by %s dry_run=%s scope=%s counts=%s total=%d", request.requested_by, report.dry_run,
                     report.scope, report.counts, report.total)
        return {"dry_run": report.dry_run, "scope": report.scope, "counts": report.counts, "total": report.total,
                "requested_by": request.requested_by}

    def close(self) -> None:
        """Close every pooled connection."""
        with self._idle_lock:
            pooled, self._idle = self._idle, []
        for mailbox in pooled:
            mailbox.close()

    # --- helpers ---------------------------------------------------------------------------------------

    @contextmanager
    def _slot(self, timeout: float) -> Iterator[None]:
        """Hold one connection slot, queued for at most ``timeout`` seconds.

        Raises:
            ImapConnectionError: ``READER_IMAP_CONNECTION_LIMIT`` when no slot became free in time.
        """
        with ExitStack() as stack:
            try:
                stack.enter_context(self._budget.slots(1, timeout=timeout))
            except TimeoutError as error:
                raise ImapConnectionError(
                    f"All {self._budget.limit} broker connections stayed busy for {timeout:g} s",
                    ReaderKeyword.IMAP_CONNECTION_LIMIT, infrastructure=True,
                    details={"limit": self._budget.limit, "queued_sec": timeout, "source": "broker budget"},
                ) from error
            yield

    def _take_mailbox(self) -> Mailbox:
        """A pooled mailbox (open connection) or a new one."""
        with self._idle_lock:
            if self._idle:
                return self._idle.pop()
        return self._mailbox_factory(self._settings.imap)

    def _return_mailbox(self, mailbox: Mailbox, healthy: bool) -> None:
        """Keep a mailbox for the next wait; a failed one is closed so the next wait reconnects."""
        if not healthy:
            mailbox.close()
        with self._idle_lock:
            self._idle.append(mailbox)

    def _open_purge(self) -> Any:
        """Open the read-write connection, with reader keywords on failure."""
        try:
            return self._purge_factory(self._settings.imap)
        except imaplib.IMAP4.error as error:
            raise classify(error, Phase.LOGIN, self._settings.imap.endpoint) from error
        except OSError as error:
            raise classify(error, Phase.CONNECT, self._settings.imap.endpoint) from error

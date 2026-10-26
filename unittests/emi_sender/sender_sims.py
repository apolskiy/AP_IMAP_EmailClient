# SPDX-FileCopyrightText: 2026 Aleksandr Polskiy
# SPDX-License-Identifier: Apache-2.0
"""Test doubles for the sender: a scripted delivery (the shared clock is in ``unittests.sims``)."""

from emi_sender.compose import ComposedEmail
from emi_sender.errors import DeliveryError
from emi_sender.keywords import SenderKeyword
from emi_sender.settings import SenderSettings


class SimOutbox:
    """Shared state of every simulated delivery: what was delivered and which failures to raise next."""

    def __init__(self) -> None:
        """Start empty."""
        self.delivered: list[ComposedEmail] = []
        self.script: list[DeliveryError] = []
        self.connections = 0
        self.closed = 0

    def factory(self, _settings: SenderSettings) -> "SimDelivery":
        """Delivery factory: one new connection per worker."""
        self.connections += 1
        return SimDelivery(self)


class SimDelivery:
    """Delivery that records messages or raises the next scripted failure."""

    def __init__(self, outbox: SimOutbox) -> None:
        """Bind to the shared outbox."""
        self._outbox = outbox

    def deliver(self, composed: ComposedEmail) -> None:
        """Record the message, or raise the next scripted error."""
        if self._outbox.script:
            raise self._outbox.script.pop(0)
        self._outbox.delivered.append(composed)

    def close(self) -> None:
        """Count closes."""
        self._outbox.closed += 1


def dropped() -> DeliveryError:
    """A healable mid-session fault."""
    return DeliveryError("dropped", SenderKeyword.SMTP_CONNECTION_DROPPED, retryable=True, infrastructure=True)


def refused() -> DeliveryError:
    """Server down."""
    return DeliveryError("refused", SenderKeyword.SMTP_CONNECTION_REFUSED, infrastructure=True)


def rejected() -> DeliveryError:
    """Recipient rejected (5xx on RCPT)."""
    return DeliveryError("rejected", SenderKeyword.SMTP_RECIPIENT_REJECTED)

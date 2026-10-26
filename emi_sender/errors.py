# SPDX-FileCopyrightText: 2026 Aleksandr Polskiy
# SPDX-License-Identifier: Apache-2.0
"""Typed sender errors; each one carries its primary keyword and a retry decision."""

from collections.abc import Mapping
from typing import Any


class SenderError(Exception):
    """Base class of every error the sender raises."""

    def __init__(
        self,
        message: str,
        primary: str,
        *,
        retryable: bool = False,
        infrastructure: bool = False,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        """Create the error.

        Args:
            message: Human-readable description, never containing credentials.
            primary: Primary keyword (the cause).
            retryable: True when a reconnect may heal the fault.
            infrastructure: True when the environment, not the request, failed.
            details: Extra diagnostic values (host, port, field, …).
        """
        super().__init__(message)
        self._primary = primary
        self._retryable = retryable
        self._infrastructure = infrastructure
        self._details = dict(details or {})

    @property
    def primary(self) -> str:
        """Primary keyword."""
        return self._primary

    @property
    def retryable(self) -> bool:
        """Whether a reconnect may heal the fault."""
        return self._retryable

    @property
    def infrastructure(self) -> bool:
        """Whether this is an environment failure."""
        return self._infrastructure

    @property
    def details(self) -> dict[str, Any]:
        """A copy of the diagnostic details."""
        return dict(self._details)


class InvalidInputError(SenderError):
    """The request is invalid (wrong type, out of range, unknown template); CLI exit code 2."""


class DeliveryError(SenderError):
    """Delivering one message failed."""

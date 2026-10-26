# SPDX-FileCopyrightText: 2026 Aleksandr Polskiy
# SPDX-License-Identifier: Apache-2.0
"""Typed reader errors; each one carries its primary and secondary keywords."""

from collections.abc import Mapping
from typing import Any


class ReaderError(Exception):
    """Base class of every error the reader raises.

    Attributes are exposed read-only so diagnostics and the harness can classify the outcome
    without parsing the message.
    """

    def __init__(
        self,
        message: str,
        primary: str,
        *,
        secondary: tuple[str, ...] = (),
        retryable: bool = False,
        infrastructure: bool = False,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        """Create the error.

        Args:
            message: Human-readable description, never containing credentials.
            primary: Primary keyword (the cause).
            secondary: Secondary keywords (context).
            retryable: True when a reconnect may heal the fault (design §5.5).
            infrastructure: True when the environment, not the test, failed.
            details: Extra diagnostic values (host, port, attempts, …).
        """
        super().__init__(message)
        self._primary = primary
        self._secondary = secondary
        self._retryable = retryable
        self._infrastructure = infrastructure
        self._details = dict(details or {})

    @property
    def primary(self) -> str:
        """Primary keyword."""
        return self._primary

    @property
    def secondary(self) -> tuple[str, ...]:
        """Secondary keywords."""
        return self._secondary

    @property
    def retryable(self) -> bool:
        """Whether a reconnect may heal the fault."""
        return self._retryable

    @property
    def infrastructure(self) -> bool:
        """Whether this is an environment failure (skip and stop the job)."""
        return self._infrastructure

    @property
    def details(self) -> dict[str, Any]:
        """A copy of the diagnostic details."""
        return dict(self._details)

    def with_details(self, **extra: Any) -> "ReaderError":
        """Return the same error with more diagnostic details merged in.

        Args:
            **extra: Details to add or replace.

        Returns:
            This error, updated in place, for chaining in ``raise`` statements.
        """
        self._details.update(extra)
        return self


class ImapConnectionError(ReaderError):
    """Network, TLS, authentication or protocol failure talking to the IMAP server."""


class MailboxAccessError(ReaderError):
    """No usable folder, or mail filtered to a folder the reader cannot open."""


class EmailWaitError(ReaderError):
    """The expected email did not arrive, or too many arrived."""


class LinkExtractionError(ReaderError):
    """The call-to-action link is missing, ambiguous or inconsistent."""


class MimeDecodeError(ReaderError):
    """An email part could not be decoded."""


class TokenError(ReaderError):
    """The link's token is missing, malformed or belongs to another recipient."""

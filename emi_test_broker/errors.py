# SPDX-FileCopyrightText: 2026 Aleksandr Polskiy
# SPDX-License-Identifier: Apache-2.0
"""Typed test broker errors; each one carries its primary and secondary keywords."""
# Each tool owns its error type and shares code only through declared dependencies, so the keyword
# carrier repeats the shape of the other tools' errors on purpose.
# pylint: disable=duplicate-code

from collections.abc import Mapping
from typing import Any


class TestBrokerError(Exception):
    """Base class of every error the test broker raises."""

    __test__ = False  # not a pytest test class despite the name

    def __init__(
        self,
        message: str,
        primary: str,
        *,
        secondary: tuple[str, ...] = (),
        infrastructure: bool = False,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        """Create the error.

        Args:
            message: Human-readable description, never containing credentials or the access token.
            primary: Primary keyword (the cause).
            secondary: Secondary keywords (context).
            infrastructure: True when the environment, not the request, failed.
            details: Extra diagnostic values (port, versions, active runs, …).
        """
        super().__init__(message)
        self._primary = primary
        self._secondary = secondary
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
    def infrastructure(self) -> bool:
        """Whether this is an environment failure (skip and stop the job)."""
        return self._infrastructure

    @property
    def details(self) -> dict[str, Any]:
        """A copy of the diagnostic details."""
        return dict(self._details)


class BrokerUnavailableError(TestBrokerError):
    """The broker does not answer, cannot open its port, or is another version."""


class PurgeRefusedError(TestBrokerError):
    """Purge refused because runs are active."""


class RemoteError(TestBrokerError):
    """An error raised inside the broker whose type has no local class (kept with its keywords)."""

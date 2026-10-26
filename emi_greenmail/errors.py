# SPDX-FileCopyrightText: 2026 Aleksandr Polskiy
# SPDX-License-Identifier: Apache-2.0
"""Typed Greenmail client errors; each one carries its primary keyword."""

from collections.abc import Mapping
from typing import Any


class GreenmailError(Exception):
    """Base class of every error the Greenmail client raises."""

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
            message: Human-readable description, never containing credentials.
            primary: Primary keyword (the cause).
            secondary: Secondary keywords (context).
            infrastructure: True when the environment, not the request, failed.
            details: Extra diagnostic values (endpoints probed, HTTP status, …).
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
        """Whether this is an environment failure."""
        return self._infrastructure

    @property
    def details(self) -> dict[str, Any]:
        """A copy of the diagnostic details."""
        return dict(self._details)


class GreenmailNotReadyError(GreenmailError):
    """The readiness check or a port probe failed; tests skip and the job stops."""


class GreenmailApiError(GreenmailError):
    """A management call (users, purge, configuration) was answered with an error."""

# SPDX-FileCopyrightText: 2026 Aleksandr Polskiy
# SPDX-License-Identifier: Apache-2.0
"""Errors of the central API, and ``EmiError``: every typed error a test can see (design §8.2).

Each tool owns its error base class and the tools cannot depend on ``emi_api``, so ``EmiError`` is the
tuple of those bases; ``except EmiError:`` catches any of them. An ABC with ``register()`` would satisfy
``isinstance`` but not ``except``, which matches real subclasses only (README, "EmiError"). Every member
carries ``primary`` and ``secondary`` keywords (the sender's errors have no secondary keywords;
:func:`keywords_of` returns ``()``).
"""
# Each tool owns its error type and shares code only through declared dependencies, so the keyword
# carrier repeats the shape of the other tools' errors on purpose.
# pylint: disable=duplicate-code

from collections.abc import Mapping
from typing import Any

from emi_greenmail import GreenmailError
from emi_reader import ReaderError
from emi_sender import SenderError
from emi_test_broker import TestBrokerError


class ApiError(Exception):
    """Base class of errors raised by the central API itself."""

    def __init__(
        self,
        message: str,
        primary: str,
        *,
        secondary: tuple[str, ...] = (),
        details: Mapping[str, Any] | None = None,
    ) -> None:
        """Create the error.

        Args:
            message: Human-readable description.
            primary: Primary keyword (the cause).
            secondary: Secondary keywords (context).
            details: Extra diagnostic values.
        """
        super().__init__(message)
        self._primary = primary
        self._secondary = secondary
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
        """API errors are condition errors, never environment failures."""
        return False

    @property
    def details(self) -> dict[str, Any]:
        """A copy of the diagnostic details."""
        return dict(self._details)


class AddressInvalidError(ApiError):
    """A test address is not ASCII ``[a-z0-9_-]`` or its local part is over 64 characters."""


# Named like a class because tests write ``except EmiError:``; ``except`` ignores virtual subclasses, so a
# tuple of the tools' bases is the only form that catches them all.
# pylint: disable-next=invalid-name
EmiError: tuple[type[Exception], ...] = (ReaderError, SenderError, TestBrokerError, GreenmailError, ApiError)


def keywords_of(error: BaseException) -> tuple[str, tuple[str, ...]]:
    """Primary and secondary keywords of any error a test may see.

    Args:
        error: The error.

    Returns:
        ``(primary, secondary)``; ``("", ())`` for errors that carry no keyword.
    """
    primary = str(getattr(error, "primary", "") or "")
    secondary = tuple(str(item) for item in getattr(error, "secondary", ()) or ())
    return primary, secondary

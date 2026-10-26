# SPDX-FileCopyrightText: 2026 Aleksandr Polskiy
# SPDX-License-Identifier: Apache-2.0
"""Run IDs, test addresses and markers (design §4).

Run ID ``<yyyymmddthhmmss>-w<worker>-<4 hex>`` (24 characters), e.g. ``20261026t153012-w03-a1b2``: readable
and searchable by time; the harness picks the timestamp and hex once per run, the worker part tells the
run's parallel workers apart. The test ID is a TestRail case ID ``C<6+ digits>`` everywhere mail carries it,
so each email maps to its TestRail case. Address ``qa+C400001_<run id>@example.com``: lower-case except
the TestRail ``C`` (recipient matching is case-insensitive), ASCII only, local part at most 64 characters
(RFC 5321).
"""

import os
import re
import secrets
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime

from emi_api.errors import AddressInvalidError
from emi_api.keywords import ApiKeyword
from emi_sender import Marker, testrail_case_id

_PART = re.compile(r"^[a-z0-9_-]+$")
_RUN_ID = re.compile(r"^(?P<stamp>\d{8}t\d{6})-w(?P<worker>\d{2,})-(?P<token>[0-9a-f]{4})$")
_ADDRESS = re.compile(r"^(?P<base>[a-z0-9.]+)\+(?P<test>c\d{6,})_(?P<run>[a-z0-9_-]+)@(?P<domain>.+)$",
                      re.IGNORECASE)
_MAX_LOCAL_PART = 64


@dataclass(frozen=True)
class RunId:
    """One worker's run ID.

    Attributes:
        stamp: Run start, ``yyyymmddthhmmss`` (UTC).
        worker: Worker number (pytest-xdist ``gw<n>``; 0 without workers).
        token: 4 random hex characters, shared by the run's workers.
    """

    stamp: str
    worker: int
    token: str

    def __post_init__(self) -> None:
        """Validate the parts.

        Raises:
            AddressInvalidError: ``API_ADDRESS_INVALID`` when a part has the wrong shape.
        """
        if not _RUN_ID.match(str(self)):
            raise AddressInvalidError(f"run ID {str(self)!r} does not match <yyyymmddthhmmss>-w<nn>-<4 hex>",
                                      ApiKeyword.ADDRESS_INVALID, details={"run_id": str(self)})

    def __str__(self) -> str:
        """The run ID text."""
        return f"{self.stamp}-w{self.worker:02d}-{self.token}"

    @classmethod
    def new(cls, worker: int = 0, now: datetime | None = None, token: str | None = None) -> "RunId":
        """Start a run ID.

        Args:
            worker: Worker number.
            now: Run start; current UTC time by default.
            token: 4 hex characters; random by default.

        Returns:
            The run ID.
        """
        moment = (now or datetime.now(UTC)).astimezone(UTC)
        return cls(moment.strftime("%Y%m%dt%H%M%S"), worker, token or secrets.token_hex(2))

    @classmethod
    def parse(cls, text: str) -> "RunId":
        """Parse a run ID.

        Args:
            text: For example ``20261026t153012-w03-a1b2``.

        Returns:
            The run ID.

        Raises:
            AddressInvalidError: If the text is not a run ID.
        """
        match = _RUN_ID.match(text.strip().lower())
        if not match:
            raise AddressInvalidError(f"{text!r} is not a run ID", ApiKeyword.ADDRESS_INVALID,
                                      details={"run_id": text})
        return cls(match["stamp"], int(match["worker"]), match["token"])

    def for_worker(self, worker: int) -> "RunId":
        """The same run, another worker.

        Args:
            worker: Worker number.

        Returns:
            The worker's run ID.
        """
        return RunId(self.stamp, worker, self.token)


def worker_number(environ: Mapping[str, str] | None = None) -> int:
    """The pytest-xdist worker number (``gw3`` → 3), or 0 without workers.

    Args:
        environ: The environment; ``os.environ`` by default.

    Returns:
        The worker number.
    """
    name = (environ if environ is not None else os.environ).get("PYTEST_XDIST_WORKER", "")
    digits = name.removeprefix("gw")
    return int(digits) if digits.isdigit() else 0


def case_id(test_id: str) -> str:
    """A test ID in TestRail case form, so each email maps to its TestRail case.

    Args:
        test_id: ``400001``, ``C400001`` or ``c400001``.

    Returns:
        ``C400001``.

    Raises:
        AddressInvalidError: ``API_ADDRESS_INVALID`` when it is not ``C`` plus at least 6 digits.
    """
    normalized = testrail_case_id(test_id) if test_id.isascii() else None
    if normalized is None:
        raise AddressInvalidError(
            f"test ID {test_id!r} must be a TestRail case ID: C followed by at least 6 digits (e.g. C400001)",
            ApiKeyword.ADDRESS_INVALID, details={"field": "test ID", "value": test_id})
    return normalized


def build_address(test_id: str, run_id: str, base_mailbox: str = "qa@example.com") -> str:
    """Build a test's own address.

    Args:
        test_id: TestRail case ID (``C400001``; ``400001`` is accepted).
        run_id: Run ID.
        base_mailbox: The shared mailbox; its local part becomes the prefix.

    Returns:
        ``<base>+C<number>_<run id>@<domain>``; everything lower-case except the TestRail ``C``.

    Raises:
        AddressInvalidError: ``API_ADDRESS_INVALID`` for a test ID that is not a TestRail case ID, a run ID
            outside ASCII ``[a-z0-9_-]`` (Cyrillic included), or a local part over 64 characters.
    """
    base_local, _sep, domain = base_mailbox.strip().lower().partition("@")
    test_part, run_part = case_id(test_id), run_id.strip().lower()
    if not run_part.isascii() or not _PART.match(run_part):
        raise AddressInvalidError(f"run ID {run_part!r} must be ASCII [a-z0-9_-] (no spaces, no Cyrillic)",
                                  ApiKeyword.ADDRESS_INVALID, details={"field": "run ID", "value": run_part})
    if not domain or not base_local:
        raise AddressInvalidError(f"base mailbox {base_mailbox!r} is not an address", ApiKeyword.ADDRESS_INVALID,
                                  details={"field": "base_mailbox", "value": base_mailbox})
    local = f"{base_local}+{test_part}_{run_part}"
    if len(local) > _MAX_LOCAL_PART:
        raise AddressInvalidError(
            f"local part {local!r} is {len(local)} characters; the limit is {_MAX_LOCAL_PART} (RFC 5321)",
            ApiKeyword.ADDRESS_INVALID, details={"field": "local_part", "length": len(local)})
    return f"{local}@{domain}"


def marker_for(address: str) -> Marker | None:
    """The marker a test's address implies (``qa+C<number>_<run id>@...``).

    Args:
        address: A recipient address.

    Returns:
        The marker (test ID in TestRail form), or None when the address is not a test address.
    """
    match = _ADDRESS.match(address.strip())
    if not match:
        return None
    return Marker(test_id=match["test"], run_id=match["run"].lower())


@dataclass(frozen=True)
class EmiRun:
    """One test's identity within a run: its ID, the run ID and its own address.

    Attributes:
        test_id: The test's TestRail case ID (``C400001``; ``400001`` is accepted and converted).
        run_id: The worker's run ID.
        base_mailbox: The shared mailbox the address is built on.
    """

    test_id: str
    run_id: str
    base_mailbox: str = "qa@example.com"

    def __post_init__(self) -> None:
        """Bring the test ID to TestRail form and validate by building the address once.

        Raises:
            AddressInvalidError: ``API_ADDRESS_INVALID``.
        """
        object.__setattr__(self, "test_id", case_id(self.test_id))
        self.address()

    def address(self) -> str:
        """The test's own address.

        Returns:
            ``qa+C<number>_<run id>@example.com``.
        """
        return build_address(self.test_id, self.run_id, self.base_mailbox)

    @property
    def marker(self) -> Marker:
        """The marker sender-generated mail carries for this test."""
        return Marker(test_id=self.test_id, run_id=self.run_id.lower())

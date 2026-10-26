# SPDX-FileCopyrightText: 2026 Aleksandr Polskiy
# SPDX-License-Identifier: Apache-2.0
"""Test outcomes and how an error becomes one (design §9.3).

Every non-pass carries one primary keyword and any secondary keywords. There is no "not run" state: a test
that did not execute is a SKIP with the triggering keyword. A skip is never a pass.
"""

from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any

from emi_harness.keywords import CaseKeyword


class Status(StrEnum):
    """Final state of one test."""

    PASSED = "passed"
    FAILED = "failed"
    SKIPPED = "skipped"


class CaseSkip(Exception):
    """Raised to end a test as SKIP with keywords (environment or condition problem)."""

    def __init__(self, message: str, primary: str, *, secondary: tuple[str, ...] = (), step: str = "",
                 infrastructure: bool = False) -> None:
        """Create the outcome.

        Args:
            message: Why the test was skipped.
            primary: Primary keyword.
            secondary: Secondary keywords.
            step: Step label (``PRE``, ``S00``, ``S01``, ...), or ``""`` outside steps.
            infrastructure: True for an environment failure, which stops the job.
        """
        super().__init__(message)
        self.primary = primary
        self.secondary = secondary
        self.step = step
        self.infrastructure = infrastructure


class CaseFailure(AssertionError):
    """Raised to end a test as FAILED with keywords."""

    def __init__(self, message: str, primary: str, *, secondary: tuple[str, ...] = (), step: str = "") -> None:
        """Create the outcome.

        Args:
            message: What failed.
            primary: Primary keyword.
            secondary: Secondary keywords.
            step: Step label, or ``""`` outside steps.
        """
        super().__init__(message)
        self.primary = primary
        self.secondary = secondary
        self.step = step


@dataclass(frozen=True)
class Verdict:
    """How an error ends a test.

    Attributes:
        status: FAILED or SKIPPED.
        primary: Primary keyword.
        secondary: Secondary keywords.
        infrastructure: Environment failure (skip and stop the job).
        message: The error's message.
    """

    status: Status
    primary: str
    secondary: tuple[str, ...]
    infrastructure: bool
    message: str


def classify(error: BaseException) -> Verdict:
    """Turn any error raised by a test into its verdict.

    Typed tool errors keep their keywords; an environment failure (``infrastructure``) becomes a SKIP; a
    ``CASE_CONDITION_*`` keyword is a SKIP (the test's own parameters are wrong); a plain ``assert`` is
    ``CASE_VERIFICATION_FAILED``; anything else untyped is ``CASE_UNEXPECTED_ERROR``.

    Args:
        error: The error.

    Returns:
        The verdict.
    """
    message = str(error) or type(error).__name__
    if isinstance(error, CaseSkip):
        return Verdict(Status.SKIPPED, error.primary, error.secondary, error.infrastructure, message)
    if isinstance(error, CaseFailure):
        return Verdict(Status.FAILED, error.primary, error.secondary, False, message)
    primary = str(getattr(error, "primary", "") or "")
    secondary = tuple(str(item) for item in getattr(error, "secondary", ()) or ())
    if primary:
        infrastructure = bool(getattr(error, "infrastructure", False))
        skipped = infrastructure or primary.startswith("CASE_CONDITION_")
        return Verdict(Status.SKIPPED if skipped else Status.FAILED, primary, secondary, infrastructure, message)
    if isinstance(error, AssertionError):
        return Verdict(Status.FAILED, CaseKeyword.VERIFICATION_FAILED, (), False, message)
    return Verdict(Status.FAILED, CaseKeyword.UNEXPECTED_ERROR, (type(error).__name__,), False, message)


@dataclass(frozen=True)
class StepRecord:
    """One step's result.

    Attributes:
        label: ``PRE``, ``S00`` (setup) or ``S01``...
        description: What the step does.
        status: PASSED, FAILED or SKIPPED.
        duration: Seconds.
        verifications: ``(description, passed)`` pairs in order.
        primary: Keyword when it did not pass.
        message: Why it did not pass.
    """

    label: str
    description: str
    status: Status
    duration: float
    verifications: tuple[tuple[str, bool], ...] = ()
    primary: str = ""
    message: str = ""


@dataclass(frozen=True)
class TestOutcome:  # pylint: disable=too-many-instance-attributes
    """One test's result, as written to the run's results file and read by the gates.

    Attributes:
        nodeid: pytest node ID.
        case_id: TestRail case ID (``C400001``).
        priority: ``P0``..``P4``.
        status: Final state.
        primary: Primary keyword ("" for a pass).
        secondary: Secondary keywords.
        step: Step label where it ended, or "".
        message: Why it did not pass.
        duration: Seconds.
        infrastructure: Environment failure.
        job: The job that ran it.
        steps: Every step's record.
        attachments: Evidence files, relative to the job's report directory.
    """

    __test__ = False

    nodeid: str
    case_id: str
    priority: str
    status: Status
    primary: str = ""
    secondary: tuple[str, ...] = ()
    step: str = ""
    message: str = ""
    duration: float = 0.0
    infrastructure: bool = False
    job: str = ""
    steps: tuple[StepRecord, ...] = field(default_factory=tuple)
    attachments: tuple[str, ...] = ()

    @property
    def passed(self) -> bool:
        """Only a pass is a pass."""
        return self.status is Status.PASSED

    def to_json(self) -> dict[str, Any]:
        """As plain JSON.

        Returns:
            A dictionary.
        """
        data = asdict(self)
        data["status"] = str(self.status)
        data["steps"] = [dict(asdict(record), status=str(record.status)) for record in self.steps]
        return data

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "TestOutcome":
        """Rebuild from :meth:`to_json`.

        Args:
            data: A dictionary.

        Returns:
            The outcome.
        """
        steps = tuple(StepRecord(label=item["label"], description=item["description"], status=Status(item["status"]),
                                 duration=float(item["duration"]),
                                 verifications=tuple((str(name), bool(passed))
                                                     for name, passed in item["verifications"]),
                                 primary=item.get("primary", ""), message=item.get("message", ""))
                      for item in data.get("steps", []))
        return cls(nodeid=data["nodeid"], case_id=data.get("case_id", ""), priority=data.get("priority", "P2"),
                   status=Status(data["status"]), primary=data.get("primary", ""),
                   secondary=tuple(data.get("secondary", ())), step=data.get("step", ""),
                   message=data.get("message", ""), duration=float(data.get("duration", 0.0)),
                   infrastructure=bool(data.get("infrastructure", False)), job=data.get("job", ""), steps=steps,
                   attachments=tuple(str(path) for path in data.get("attachments", ())))

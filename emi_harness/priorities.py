# SPDX-FileCopyrightText: 2026 Aleksandr Polskiy
# SPDX-License-Identifier: Apache-2.0
"""Priorities, downgrades on overflow and the quality gates (design §9.7).

* P0 = critical, P1 = high, P2-P4 = standard / low. P0 + P1 may be at most 20% of the tests.
* Overflow: the excess **P1** tests move to P2 for this run (lowest declared ``rank`` first, then highest
  test number), each logged as ``JOB_PRIORITY_DOWNGRADED``. P0 is never moved; P0 alone above 20% is
  ``JOB_PRIORITY_P0_OVERFLOW``.
* Gates: P0 = 100%, P1 = 100%; P2-P4 together >= 85% (otherwise nothing else is measured); all passed /
  all functional >= 95%. A skip is never a pass.
* Jobs: P0, P1, then P2, P3 and P4 (in CI side by side, AMB-67), or P2-P4 as one job (locally).
"""

import math
import re
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum

from emi_harness.keywords import JobKeyword
from emi_harness.outcomes import TestOutcome

PRIORITIES = ("P0", "P1", "P2", "P3", "P4")
DEFAULT_PRIORITY = "P2"
MUST_PASS_SHARE = 0.20
LOW_GATE = 0.85
OVERALL_GATE = 0.95
_NUMBER = re.compile(r"(\d+)")


class Job(StrEnum):
    """A functional job: which priorities it runs and which stop rules apply (design §9.4)."""

    P0 = "P0"
    P1 = "P1"
    P2 = "P2"
    P3 = "P3"
    P4 = "P4"
    P2_P4 = "P2-P4"
    DEBUG = "debug"
    ALL = "all"

    @property
    def priorities(self) -> tuple[str, ...]:
        """Priorities the job runs."""
        if self in (Job.P0, Job.P1, Job.P2, Job.P3, Job.P4):
            return (str(self),)
        if self is Job.P2_P4:
            return ("P2", "P3", "P4")
        return PRIORITIES

    @property
    def low(self) -> bool:
        """A job of standard / low tests (P2, P3, P4, or the three together)."""
        return self in LOW_JOBS

    @property
    def breaker(self) -> bool:
        """Whether the circuit breaker applies (the low jobs and debug runs)."""
        return self.low or self in (Job.DEBUG, Job.ALL)


LOW_JOBS = (Job.P2, Job.P3, Job.P4, Job.P2_P4)


def job_name(value: str) -> str:
    """A job name in its canonical case, for command lines that accept any case.

    Args:
        value: A job name as typed (``p3``, ``P2-p4``, ``ALL``).

    Returns:
        The job's own name (``P3``, ``P2-P4``, ``all``); an unknown name unchanged, for argparse to reject.
    """
    return next((str(job) for job in Job if str(job).lower() == value.strip().lower()), value)


def parse_priority(value: str | None) -> str:
    """Validate a declared priority.

    Args:
        value: ``P0``..``P4`` (any case), or None for the default.

    Returns:
        The priority.

    Raises:
        ValueError: If it is not one of P0-P4.
    """
    if value is None:
        return DEFAULT_PRIORITY
    priority = value.strip().upper()
    if priority not in PRIORITIES:
        raise ValueError(f"priority must be one of {', '.join(PRIORITIES)}, got {value!r}")
    return priority


@dataclass(frozen=True)
class Planned:
    """One collected test as the priority plan sees it.

    Attributes:
        nodeid: pytest node ID.
        case_id: TestRail case ID.
        priority: Declared priority.
        rank: Declared importance within P1 (higher stays longer).
    """

    nodeid: str
    case_id: str
    priority: str
    rank: int = 0


@dataclass(frozen=True)
class Downgrade:
    """A P1 test moved to P2 for this run.

    Attributes:
        nodeid: pytest node ID.
        case_id: TestRail case ID.
    """

    nodeid: str
    case_id: str


@dataclass(frozen=True)
class PriorityPlan:
    """Effective priorities after downgrades.

    Attributes:
        priorities: node ID -> effective priority.
        downgrades: The moves made, in order.
        p0_overflow: P0 alone exceeds the share.
        allowed: The most P0 + P1 tests allowed.
    """

    priorities: dict[str, str]
    downgrades: tuple[Downgrade, ...] = ()
    p0_overflow: bool = False
    allowed: int = 0

    def message(self) -> str:
        """A summary for the log.

        Returns:
            One line per decision, or "".
        """
        lines = []
        if self.p0_overflow:
            count = sum(1 for value in self.priorities.values() if value == "P0")
            lines.append(f"{JobKeyword.PRIORITY_P0_OVERFLOW}: {count} P0 tests; at most {self.allowed} "
                         f"({MUST_PASS_SHARE:.0%}) may be P0 + P1. Re-label P0 tests in the source.")
        for move in self.downgrades:
            lines.append(f"{JobKeyword.PRIORITY_DOWNGRADED}: {move.case_id or move.nodeid} P1 -> P2 for this run; "
                         "suggest re-labelling it P2 in the source")
        return "\n".join(lines)


def _number(case_id: str) -> int:
    """The test number in a case ID (0 when there is none)."""
    match = _NUMBER.search(case_id)
    return int(match.group(1)) if match else 0


def plan_priorities(tests: Sequence[Planned]) -> PriorityPlan:
    """Apply the 20% rule to the collected functional tests.

    Args:
        tests: Every collected functional test.

    Returns:
        Effective priorities and the decisions made.
    """
    priorities = {item.nodeid: item.priority for item in tests}
    allowed = math.floor(len(tests) * MUST_PASS_SHARE)
    p0_count = sum(1 for item in tests if item.priority == "P0")
    if p0_count > allowed:
        return PriorityPlan(priorities, p0_overflow=True, allowed=allowed)
    excess = p0_count + sum(1 for item in tests if item.priority == "P1") - allowed
    if excess <= 0:
        return PriorityPlan(priorities, allowed=allowed)
    order = sorted((item for item in tests if item.priority == "P1"),
                   key=lambda item: (item.rank, -_number(item.case_id), item.nodeid))
    moves = tuple(Downgrade(item.nodeid, item.case_id) for item in order[:excess])
    for move in moves:
        priorities[move.nodeid] = "P2"
    return PriorityPlan(priorities, downgrades=moves, allowed=allowed)


@dataclass(frozen=True)
class GateCheck:
    """One gate's result.

    Attributes:
        name: Gate name.
        passed: Count passed.
        total: Count measured.
        required: Required share.
        holds: Whether the gate holds.
    """

    name: str
    passed: int
    total: int
    required: float
    holds: bool

    @property
    def share(self) -> float:
        """Passed share (1.0 for an empty slice)."""
        return self.passed / self.total if self.total else 1.0


@dataclass(frozen=True)
class GateReport:
    """All gates over a run's functional results.

    Attributes:
        checks: The gates measured, in order.
        effective_low_floor: The P2-P4 pass share the overall gate actually needs.
        keywords: Primary keyword -> count over every non-pass.
        must_pass_time_share: Share of test time spent in P0 + P1.
    """

    checks: tuple[GateCheck, ...]
    effective_low_floor: float
    keywords: dict[str, int] = field(default_factory=dict)
    must_pass_time_share: float = 0.0

    @property
    def holds(self) -> bool:
        """Whether every gate holds."""
        return all(check.holds for check in self.checks)

    def render(self) -> str:
        """The gate report as text (job log).

        Returns:
            Lines of text.
        """
        lines = ["Quality gates:"]
        for check in self.checks:
            lines.append(f"  {'PASS' if check.holds else 'FAIL'} {check.name}: {check.passed}/{check.total} "
                         f"({check.share:.1%}, required {check.required:.0%})")
        lines.append(f"  Effective P2-P4 floor for the overall gate: {self.effective_low_floor:.1%}")
        lines.append(f"  P0 + P1 share of test time: {self.must_pass_time_share:.1%} "
                     f"(target <= {MUST_PASS_SHARE:.0%})")
        if self.keywords:
            lines.append("Keyword summary (non-passing tests):")
            for keyword, count in sorted(self.keywords.items(), key=lambda pair: (-pair[1], pair[0])):
                lines.append(f"  {count:5d}  {keyword}")
        lines.append(f"Result: {'PASS' if self.holds else 'FAIL'}")
        return "\n".join(lines)


def _check(name: str, results: Sequence[TestOutcome], required: float) -> GateCheck:
    """One gate over a slice."""
    passed = sum(1 for item in results if item.passed)
    total = len(results)
    holds = (passed / total if total else 1.0) >= required
    return GateCheck(name, passed, total, required, holds)


def evaluate_gates(outcomes: Iterable[TestOutcome]) -> GateReport:
    """Measure the gates over every functional result of a run (the last result per test wins).

    Args:
        outcomes: Results of every job of the run.

    Returns:
        The report.
    """
    latest = {item.nodeid: item for item in outcomes}
    results = list(latest.values())
    by_priority = {name: [item for item in results if item.priority == name] for name in ("P0", "P1")}
    low = [item for item in results if item.priority in ("P2", "P3", "P4")]
    checks = [_check("P0 (100%)", by_priority["P0"], 1.0), _check("P1 (100%)", by_priority["P1"], 1.0)]
    low_check = _check("P2-P4 (>= 85%)", low, LOW_GATE)
    checks.append(low_check)
    if low_check.holds:
        checks.append(_check("Overall (>= 95%)", results, OVERALL_GATE))
    allowed_failures = len(results) - math.ceil(len(results) * OVERALL_GATE)
    must_fail = sum(1 for item in by_priority["P0"] + by_priority["P1"] if not item.passed)
    floor = 1.0 - max(0, allowed_failures - must_fail) / len(low) if low else 1.0
    keywords = Counter(item.primary for item in results if not item.passed and item.primary)
    total_time = sum(item.duration for item in results)
    must_time = sum(item.duration for item in by_priority["P0"] + by_priority["P1"])
    return GateReport(tuple(checks), max(LOW_GATE, floor), dict(keywords),
                      must_time / total_time if total_time else 0.0)

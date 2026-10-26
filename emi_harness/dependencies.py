# SPDX-FileCopyrightText: 2026 Aleksandr Polskiy
# SPDX-License-Identifier: Apache-2.0
"""Dependency skipping (design §9.5, framework-rules §6).

A test declares its parents by TestRail case ID (``@pytest.mark.emi(depends=["C400001"])``). A failed **or
skipped** parent skips its dependents, transitively; each skip carries ``CASE_DEPENDENCY_SKIPPED`` and names
the test at the top of the chain and its keyword. A parent runs under the same or milder conditions, so it
lives in the same or a higher-priority job: results of earlier jobs of the same run count. A parent that was
not run at all does not skip its children.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from emi_harness.keywords import CaseKeyword
from emi_harness.outcomes import CaseSkip, Status, TestOutcome


@dataclass(frozen=True)
class Root:
    """The test at the top of a failed chain.

    Attributes:
        case_id: Its TestRail case ID.
        status: FAILED or SKIPPED.
        primary: Its primary keyword.
    """

    case_id: str
    status: Status
    primary: str


class DependencyTracker:
    """Remembers results by case ID and answers whether a test must be skipped."""

    def __init__(self, depends: Mapping[str, Sequence[str]], earlier: Sequence[TestOutcome] = ()) -> None:
        """Create the tracker.

        Args:
            depends: case ID -> its parents' case IDs.
            earlier: Results of earlier jobs of the same run.

        Raises:
            ValueError: If the dependencies contain a cycle.
        """
        self._depends = {key: tuple(value) for key, value in depends.items()}
        self._roots: dict[str, Root | None] = {}
        for outcome in earlier:
            self.record(outcome)
        order_tests(self._depends)

    def record(self, outcome: TestOutcome) -> None:
        """Remember a finished test.

        Args:
            outcome: Its result.
        """
        if not outcome.case_id:
            return
        if outcome.passed:
            self._roots[outcome.case_id] = None
            return
        inherited = self._root_of_parents(outcome.case_id)
        self._roots[outcome.case_id] = inherited or Root(outcome.case_id, outcome.status, outcome.primary)

    def check(self, case_id: str) -> None:
        """Raise when a parent failed or was skipped.

        Args:
            case_id: The test about to run.

        Raises:
            CaseSkip: ``CASE_DEPENDENCY_SKIPPED`` naming the top of the chain and its keyword.
        """
        root = self._root_of_parents(case_id)
        if root is not None:
            raise CaseSkip(f"depends on {root.case_id}, which {root.status} with {root.primary or 'no keyword'}",
                           CaseKeyword.DEPENDENCY_SKIPPED, secondary=(root.primary,) if root.primary else ())

    def _root_of_parents(self, case_id: str) -> Root | None:
        """The first failed root among a test's parents."""
        for parent in self._depends.get(case_id, ()):
            root = self._roots.get(parent)
            if root is not None:
                return root
        return None


def order_tests(depends: Mapping[str, Sequence[str]], cases: Sequence[str] | None = None) -> list[str]:
    """Order case IDs so every parent comes before its children; otherwise keep the given order.

    Args:
        depends: case ID -> parents.
        cases: The case IDs in collection order; every key of ``depends`` by default.

    Returns:
        The ordered case IDs.

    Raises:
        ValueError: If the dependencies contain a cycle.
    """
    pending = list(cases if cases is not None else depends)
    present = set(pending)
    done: set[str] = set()
    ordered: list[str] = []
    visiting: set[str] = set()

    def _visit(case_id: str) -> None:
        if case_id in done:
            return
        if case_id in visiting:
            raise ValueError(f"dependency cycle through {case_id}")
        visiting.add(case_id)
        for parent in depends.get(case_id, ()):
            if parent in present:
                _visit(parent)
        visiting.discard(case_id)
        done.add(case_id)
        ordered.append(case_id)

    for case_id in pending:
        _visit(case_id)
    return ordered


def dependency_groups(depends: Mapping[str, Sequence[str]]) -> dict[str, str]:
    """Connected tests share a group, so pytest-xdist (``--dist loadgroup``) runs a chain on one worker.

    Args:
        depends: case ID -> parents.

    Returns:
        case ID -> group name (the smallest case ID of its group), for every test in a chain.
    """
    leader: dict[str, str] = {}

    def _find(case_id: str) -> str:
        leader.setdefault(case_id, case_id)
        while leader[case_id] != case_id:
            leader[case_id] = leader[leader[case_id]]
            case_id = leader[case_id]
        return case_id

    for child, parents in depends.items():
        for parent in parents:
            first, second = sorted((_find(child), _find(parent)))
            leader[second] = first
    return {case_id: _find(case_id) for case_id in list(leader)}

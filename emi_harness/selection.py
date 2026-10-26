# SPDX-FileCopyrightText: 2026 Aleksandr Polskiy
# SPDX-License-Identifier: Apache-2.0
"""Test identity and selection (design §4, §9.1, framework-rules §1.5).

* A test's TestRail case ID comes from ``@pytest.mark.emi(case="C400001")`` or from the number in its name
  (``EMI_TOOL_400001_waits_for_activation_email`` -> ``C400001``).
* ``--test`` accepts a function name, a case ID or part of a node ID; ``--file`` one of those per line
  (``#`` starts a comment); ``--keyword`` a full keyword or a prefix, matched against the keywords a test
  declares and the primary and secondary keywords of its last result.
"""

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from emi_harness.outcomes import TestOutcome
from emi_sender import testrail_case_id

_NAME_ID = re.compile(r"^EMI_[A-Z0-9_]*?_(\d{6,})_")


def case_id_for(name: str, declared: str | None = None) -> str:
    """The TestRail case ID of a test.

    Args:
        name: Function name without parameters.
        declared: ``case=`` from the ``emi`` marker, if any.

    Returns:
        ``C400001``, or ``""`` when the test has no ID.

    Raises:
        ValueError: If the declared case is not a TestRail case ID.
    """
    if declared:
        normalized = testrail_case_id(declared)
        if normalized is None:
            raise ValueError(f"emi(case={declared!r}) is not a TestRail case ID (C followed by 6+ digits)")
        return normalized
    match = _NAME_ID.match(name)
    return f"C{match.group(1)}" if match else ""


@dataclass(frozen=True)
class Candidate:
    """What selection knows about one collected test.

    Attributes:
        nodeid: pytest node ID.
        name: Function name.
        case_id: TestRail case ID.
        keywords: Keywords the test declares.
    """

    nodeid: str
    name: str
    case_id: str
    keywords: tuple[str, ...] = field(default_factory=tuple)


def read_test_file(path: Path) -> list[str]:
    """Names from a batch file: one per line, blank lines and ``#`` comments ignored.

    Args:
        path: The file.

    Returns:
        The names.
    """
    names = []
    for line in path.read_text(encoding="utf-8").splitlines():
        text = line.split("#", 1)[0].strip()
        if text:
            names.append(text)
    return names


def _name_matches(candidate: Candidate, wanted: str) -> bool:
    """Whether one ``--test`` value names this test."""
    case = testrail_case_id(wanted)
    if case is not None and candidate.case_id == case:
        return True
    return wanted in (candidate.name, candidate.nodeid) or (("::" in wanted or "/" in wanted)
                                                            and wanted in candidate.nodeid)


def _keyword_matches(keywords: Iterable[str], wanted: Sequence[str]) -> bool:
    """Whether any keyword equals or starts with any wanted value."""
    return any(keyword == item or keyword.startswith(item) for keyword in keywords for item in wanted if keyword)


def select(
    candidates: Sequence[Candidate],
    *,
    names: Sequence[str] = (),
    keywords: Sequence[str] = (),
    history: Mapping[str, TestOutcome] | None = None,
) -> tuple[list[Candidate], list[str]]:
    """Choose the tests to run.

    Args:
        candidates: Every collected test.
        names: ``--test`` / ``--file`` values; empty selects all.
        keywords: ``--keyword`` values (full or prefix); empty selects all.
        history: node ID -> last result, for keyword matching.

    Returns:
        The chosen tests, and the names that matched nothing (reported to the user).
    """
    chosen = list(candidates)
    unmatched: list[str] = []
    if names:
        unmatched = [wanted for wanted in names if not any(_name_matches(item, wanted) for item in candidates)]
        chosen = [item for item in chosen if any(_name_matches(item, wanted) for wanted in names)]
    if keywords:
        wanted_keywords = [item.strip().upper() for item in keywords if item.strip()]
        history = history or {}

        def _tied(item: Candidate) -> list[str]:
            last = history.get(item.nodeid)
            return list(item.keywords) + ([last.primary, *last.secondary] if last else [])

        chosen = [item for item in chosen if _keyword_matches(_tied(item), wanted_keywords)]
    return chosen, unmatched


def duplicate_ids(entries: Iterable[tuple[str, str]]) -> dict[str, list[str]]:
    """Case IDs used by more than one test.

    Args:
        entries: ``(case ID, owner)`` pairs; owner is the function for unit tests and the node ID for
            functional tests, whose parametrized cases each need their own ID.

    Returns:
        case ID -> its owners, for every duplicate.
    """
    owners: dict[str, list[str]] = {}
    for case_id, owner in entries:
        if case_id and owner not in owners.setdefault(case_id, []):
            owners[case_id].append(owner)
    return {case_id: names for case_id, names in owners.items() if len(names) > 1}

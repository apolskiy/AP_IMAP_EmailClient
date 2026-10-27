# SPDX-FileCopyrightText: 2026 Aleksandr Polskiy
# SPDX-License-Identifier: Apache-2.0
"""Allure labels for priorities and Allure categories for failure keywords (design §9.8, AMB-69).

Allure's Categories tab classifies only failed, broken and skipped results, by status and by a regular
expression on the result's message; it cannot read labels, and passed tests never appear in it. So the
two views are split:

* **Priority** is a label on every test: ``parentSuite`` P0 ... P4 (the Suites tab groups every test,
  passed or not, by priority), ``severity`` (the Graphs tab) and a ``tag``.
* **Failure cause** is a category: the harness writes each non-passing result's message as
  ``[KEYWORD] message`` and puts ``categories.json`` next to the results, one category per keyword family.

The message is taken from the harness verdict at teardown, because Allure would otherwise record the
exception text without the keyword, and nothing for failures the harness itself decides (a test that
verifies nothing, a test over its time limit).
"""

import json
from pathlib import Path
from typing import Any

import pytest

from emi_harness.outcomes import Status, TestOutcome

CATEGORIES_FILE = "categories.json"
_SEVERITY = {"P0": "blocker", "P1": "critical", "P2": "normal", "P3": "minor", "P4": "trivial"}
_NOT_PASSED = ["failed", "broken", "skipped"]
_CASE_SPECIFIC = ("EMAIL_NOT_RECEIVED", "EMAIL_COUNT_EXCEEDED", "VERIFICATION_FAILED", "DEPENDENCY_SKIPPED",
                  "LINK_[A-Z_]+")
# Allure matches the whole message (Java ``matches``), so every pattern ends in ``.*`` and spans lines.
CATEGORIES: list[dict[str, Any]] = [
    {"name": "Greenmail", "matchedStatuses": _NOT_PASSED, "messageRegex": r"(?s)\[GREENMAIL_.*"},
    {"name": "Test broker", "matchedStatuses": _NOT_PASSED, "messageRegex": r"(?s)\[TEST_BROKER_.*"},
    {"name": "Reader tool", "matchedStatuses": _NOT_PASSED, "messageRegex": r"(?s)\[READER_.*"},
    {"name": "Sender tool", "matchedStatuses": _NOT_PASSED, "messageRegex": r"(?s)\[SENDER_.*"},
    {"name": "Public API", "matchedStatuses": _NOT_PASSED, "messageRegex": r"(?s)\[API_.*"},
    {"name": "Harness", "matchedStatuses": _NOT_PASSED, "messageRegex": r"(?s)\[HARNESS_.*"},
    {"name": "Job stopped", "matchedStatuses": _NOT_PASSED, "messageRegex": r"(?s)\[JOB_.*"},
    {"name": "Skipped: a parent test did not pass", "matchedStatuses": _NOT_PASSED,
     "messageRegex": r"(?s)\[CASE_DEPENDENCY_SKIPPED\].*"},
    {"name": "Email not received", "matchedStatuses": _NOT_PASSED,
     "messageRegex": r"(?s)\[CASE_EMAIL_NOT_RECEIVED\].*"},
    {"name": "More emails than expected", "matchedStatuses": _NOT_PASSED,
     "messageRegex": r"(?s)\[CASE_EMAIL_COUNT_EXCEEDED\].*"},
    {"name": "Link problems", "matchedStatuses": _NOT_PASSED, "messageRegex": r"(?s)\[CASE_LINK_[A-Z_]+\].*"},
    {"name": "Verification failed", "matchedStatuses": _NOT_PASSED,
     "messageRegex": r"(?s)\[CASE_VERIFICATION_FAILED\].*"},
    {"name": "Other test-case failures", "matchedStatuses": _NOT_PASSED,
     "messageRegex": r"(?s)\[CASE_(?!(?:" + "|".join(_CASE_SPECIFIC) + r")\])[A-Z_]+\].*"},
]


def priority_marks(priority: str) -> list[pytest.MarkDecorator]:
    """The Allure labels of a test of this priority, as the markers allure-pytest reads.

    Args:
        priority: ``P0`` ... ``P4``.

    Returns:
        ``parentSuite``, ``severity`` and ``tag`` labels.
    """
    label = pytest.mark.allure_label
    return [label(priority, label_type="parentSuite"), label(_SEVERITY.get(priority, "normal"), label_type="severity"),
            label(priority, label_type="tag")]


def write_categories(results_dir: Path) -> None:
    """Put ``categories.json`` into an Allure results directory.

    Args:
        results_dir: The ``--alluredir`` directory.
    """
    results_dir.mkdir(parents=True, exist_ok=True)
    (results_dir / CATEGORIES_FILE).write_text(json.dumps(CATEGORIES, indent=2), encoding="utf-8")


def record_outcome(outcome: TestOutcome) -> None:
    """Give the running Allure result the harness verdict: its status and ``[KEYWORD] message``.

    Does nothing for a passed test, or when allure-pytest is not recording this test.

    Args:
        outcome: The test's final outcome.
    """
    if outcome.status is Status.PASSED or not outcome.primary:
        return
    result = _current_result()
    if result is None:
        return
    from allure_commons.model2 import StatusDetails  # pylint: disable=import-outside-toplevel
    result.status = "broken" if outcome.infrastructure else str(outcome.status).lower()
    trace = result.statusDetails.trace if result.statusDetails else None
    step = f" step {outcome.step}" if outcome.step else ""
    secondary = f" (secondary: {', '.join(outcome.secondary)})" if outcome.secondary else ""
    result.statusDetails = StatusDetails(message=f"[{outcome.primary}]{step} {outcome.message}{secondary}",
                                         trace=trace)


def _current_result() -> Any:
    """allure-pytest's result of the test now running, or None."""
    try:
        import allure_commons  # pylint: disable=import-outside-toplevel
    except ImportError:
        return None
    for plugin in allure_commons.plugin_manager.get_plugins():
        reporter = getattr(plugin, "allure_logger", None)
        if reporter is not None:
            return reporter.get_test(None)
    return None

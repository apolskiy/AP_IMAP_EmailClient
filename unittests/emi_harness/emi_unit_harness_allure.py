# SPDX-FileCopyrightText: 2026 Aleksandr Polskiy
# SPDX-License-Identifier: Apache-2.0
"""Unit tests: Allure priority labels and failure categories (design §9.8, AMB-69)."""

import json
import re
from enum import StrEnum
from pathlib import Path
from types import ModuleType

import pytest

from emi_api import keywords as api_keywords
from emi_greenmail import keywords as greenmail_keywords
from emi_harness import Status, TestOutcome
from emi_harness import allure_report
from emi_harness import keywords as harness_keywords
from emi_harness.allure_report import CATEGORIES, CATEGORIES_FILE, priority_marks, record_outcome, write_categories
from emi_reader import keywords as reader_keywords
from emi_sender import keywords as sender_keywords
from emi_test_broker import keywords as broker_keywords

_KEYWORD_MODULES: tuple[ModuleType, ...] = (api_keywords, greenmail_keywords, harness_keywords, reader_keywords,
                                            sender_keywords, broker_keywords)


def _all_keywords() -> list[str]:
    """Every keyword any tool or the harness can raise."""
    return sorted({str(member) for module in _KEYWORD_MODULES for value in vars(module).values()
                   if isinstance(value, type) and issubclass(value, StrEnum) and value is not StrEnum
                   for member in value})


def _matching(message: str, status: str) -> list[str]:
    """The categories whose status and message pattern match, as Allure's whole-message match does."""
    return [category["name"] for category in CATEGORIES
            if status in category["matchedStatuses"] and re.fullmatch(category["messageRegex"], message)]


class _SimResult:  # pylint: disable=too-few-public-methods
    """The attributes of an allure-pytest result that the harness sets."""

    def __init__(self) -> None:
        """A failed result as allure-pytest records it: the exception text, without the keyword."""
        self.status = "failed"
        self.statusDetails = None  # pylint: disable=invalid-name  # allure-pytest's attribute name


class TestEMIHarnessAllure:
    """Priorities become labels; failure keywords become categories."""

    def EMI_HARNESS_UNIT_200149_priority_labels(self) -> None:
        """Each priority is the parent suite and a tag; its severity runs from blocker (P0) to trivial (P4)."""
        severities = []
        for priority in ("P0", "P1", "P2", "P3", "P4"):
            labels = {mark.kwargs["label_type"]: mark.args[0] for mark in priority_marks(priority)}
            assert labels["parentSuite"] == labels["tag"] == priority
            severities.append(labels["severity"])
        assert severities == ["blocker", "critical", "normal", "minor", "trivial"]

    def EMI_HARNESS_UNIT_200150_every_keyword_in_one_category(self) -> None:
        """Every keyword, as ``[KEYWORD] message``, falls into exactly one category, for each non-passed status."""
        keywords = _all_keywords()
        assert len(keywords) > 40
        for keyword in keywords:
            for status in ("failed", "broken", "skipped"):
                matched = _matching(f"[{keyword}] step S02 a message\nover two lines", status)
                assert len(matched) == 1, (keyword, status, matched)
        assert not _matching("[CASE_EMAIL_NOT_RECEIVED] message", "passed")

    def EMI_HARNESS_UNIT_200151_categories_written(self, tmp_path: Path) -> None:
        """``categories.json`` is written into the results directory, creating it."""
        write_categories(tmp_path / "allure-results")
        written = json.loads((tmp_path / "allure-results" / CATEGORIES_FILE).read_text(encoding="utf-8"))
        assert written == CATEGORIES

    def EMI_HARNESS_UNIT_200152_outcome_recorded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The running Allure result gets the verdict: ``[KEYWORD] step message (secondary)``; broken if infra."""
        result = _SimResult()
        monkeypatch.setattr(allure_report, "_current_result", lambda: result)
        record_outcome(TestOutcome(nodeid="tests/emi_x.py::case", case_id="C400001", priority="P0",
                                   status=Status.FAILED, primary="CASE_NO_VERIFICATION", step="S01",
                                   message="checks nothing", secondary=("READER_IMAP_TIMEOUT",)))
        assert result.status == "failed"
        assert result.statusDetails.message == "[CASE_NO_VERIFICATION] step S01 checks nothing (secondary: " \
                                               "READER_IMAP_TIMEOUT)"
        record_outcome(TestOutcome(nodeid="tests/emi_x.py::case", case_id="C400001", priority="P0",
                                   status=Status.SKIPPED, primary="GREENMAIL_NOT_READY", message="down",
                                   infrastructure=True))
        assert result.status == "broken"
        passed = _SimResult()
        monkeypatch.setattr(allure_report, "_current_result", lambda: passed)
        record_outcome(TestOutcome(nodeid="tests/emi_x.py::case", case_id="C400001", priority="P0",
                                   status=Status.PASSED))
        assert passed.statusDetails is None

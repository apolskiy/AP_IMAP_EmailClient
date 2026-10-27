# SPDX-FileCopyrightText: 2026 Aleksandr Polskiy
# SPDX-License-Identifier: Apache-2.0
"""Unit tests: job counts in the log and the GitHub step summary (design §9.8, AMB-72)."""

import json
from pathlib import Path

import pytest

from emi_harness import Status, TestOutcome
from emi_harness.gate import main as gate_main
from emi_harness.gate import missing_results
from emi_harness.results import ResultsWriter, load_results
from emi_harness.summary import LISTED_LIMIT, append_step_summary, counts_markdown, job_counts, report_job


def _outcome(number: int, status: Status, primary: str = "", message: str = "") -> TestOutcome:
    """One P2 result."""
    return TestOutcome(nodeid=f"tests/emi_func_demo.py::C{number}", case_id=f"C{number}", priority="P2",
                       status=status, primary=primary, step="S01" if primary else "", message=message, job="P2")


def _job(directory: Path, outcomes: list[TestOutcome], selected: int) -> None:
    """Write a P2 job's results and a collection selecting the first ``selected`` numbers from 400001."""
    writer = ResultsWriter(directory, "main")
    for outcome in outcomes:
        writer.write(outcome)
    entries = [{"nodeid": f"tests/emi_func_demo.py::C{400001 + index}", "case_id": f"C{400001 + index}",
                "priority": "P2"} for index in range(selected)]
    (directory / "collection.json").write_text(json.dumps({"selected": entries}), encoding="utf-8")


class TestEMIHarnessSummary:
    """Each job reports how many tests it selected, passed, failed, skipped and lost."""

    def EMI_HARNESS_UNIT_200153_counts_match_results(self, tmp_path: Path) -> None:
        """Counts come from the harness verdicts; a selected test without a result is counted on its own."""
        _job(tmp_path, [_outcome(400001, Status.PASSED), _outcome(400002, Status.PASSED),
                        _outcome(400003, Status.FAILED, "CASE_EMAIL_NOT_RECEIVED", "no email"),
                        _outcome(400004, Status.SKIPPED, "CASE_DEPENDENCY_FAILED", "parent failed")], selected=5)
        counts = job_counts("P2", load_results(tmp_path), missing_results(tmp_path))
        assert counts.line() == "P2 job: 5 selected, 2 passed, 1 failed, 1 skipped, 1 without a result"
        assert [outcome.case_id for outcome in counts.not_passed] == ["C400003", "C400004", "C400005"]

    def EMI_HARNESS_UNIT_200154_last_result_counts_once(self) -> None:
        """A test written twice (a rerun) counts once, by its last result."""
        counts = job_counts("P3", [_outcome(400001, Status.FAILED, "CASE_TIMEOUT"),
                                   _outcome(400001, Status.PASSED)], [])
        assert (counts.selected, counts.passed, counts.failed) == (1, 1, 0)

    def EMI_HARNESS_UNIT_200155_markdown_titles_os_and_lists(self) -> None:
        """The section is titled with job and OS, lists not-passed tests up to the limit, then the rest's count."""
        failures = [_outcome(400001 + index, Status.FAILED, "CASE_LINK_MISMATCH", "a | b\nnext line")
                    for index in range(LISTED_LIMIT + 3)]
        markdown = counts_markdown(job_counts("P4", failures, []), {"RUNNER_OS": "Windows"})
        assert markdown.startswith("### P4 job (Windows)\n")
        assert f"| {LISTED_LIMIT + 3} | 0 | {LISTED_LIMIT + 3} | 0 | 0 |" in markdown
        assert markdown.count("| CASE_LINK_MISMATCH |") == LISTED_LIMIT
        assert "| a \\| b next line |" in markdown  # a cell never breaks the table
        assert "3 more not passed; see `report.html`" in markdown
        clean = counts_markdown(job_counts("P0", [_outcome(400001, Status.PASSED)], []), {})
        assert clean.startswith("### P0 job\n") and "| Test |" not in clean

    def EMI_HARNESS_UNIT_200156_step_summary_only_in_ci(self, tmp_path: Path,
                                                        capsys: pytest.CaptureFixture[str]) -> None:
        """The line is always printed; the section is appended only when GITHUB_STEP_SUMMARY names a file,
        and a file that cannot be written only warns."""
        counts = job_counts("P1", [_outcome(400001, Status.PASSED)], [])
        target = tmp_path / "summary.md"
        report_job(counts, {})
        assert not target.exists()
        report_job(counts, {"GITHUB_STEP_SUMMARY": str(target), "RUNNER_OS": "Linux"})
        report_job(counts, {"GITHUB_STEP_SUMMARY": str(target), "RUNNER_OS": "Linux"})
        assert target.read_text(encoding="utf-8").count("### P1 job (Linux)") == 2  # appended, not replaced
        assert capsys.readouterr().out.count("P1 job: 1 selected, 1 passed") == 3
        append_step_summary("text", {"GITHUB_STEP_SUMMARY": str(tmp_path)})  # a directory: cannot be opened
        assert "warning: step summary not written" in capsys.readouterr().err

    def EMI_HARNESS_UNIT_200157_gate_writes_step_summary(self, tmp_path: Path,
                                                         monkeypatch: pytest.MonkeyPatch) -> None:
        """The gate appends its verdict, absent jobs and report to the step summary."""
        _job(tmp_path / "ubuntu" / "P2", [_outcome(400001, Status.PASSED)], selected=1)
        target = tmp_path / "summary.md"
        monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(target))
        assert gate_main([str(tmp_path / "ubuntu"), "--expect", "P2", "--expect", "P3"]) == 1
        summary = target.read_text(encoding="utf-8")
        assert summary.startswith("### Quality gate: FAIL\n")
        assert "JOB_RESULT_MISSING: job P3 left no collection.json" in summary
        assert "Result: PASS" in summary  # the measured shares hold; the absent job alone fails the gate

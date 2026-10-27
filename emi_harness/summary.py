# SPDX-FileCopyrightText: 2026 Aleksandr Polskiy
# SPDX-License-Identifier: Apache-2.0
"""Job counts for the log and the GitHub run page (design §9.8, AMB-72).

A CI job's log otherwise shows only pytest's own line, and the run page only a green or red mark. The counts
come from the job's harness results, so they agree with the gates. When ``GITHUB_STEP_SUMMARY`` is set, the
counts and the tests that did not pass are appended to it as Markdown; GitHub shows that under the job.
"""

import os
import sys
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

from emi_harness.outcomes import Status, TestOutcome

LISTED_LIMIT = 50
_SUMMARY_VARIABLE = "GITHUB_STEP_SUMMARY"
_MESSAGE_LIMIT = 200


@dataclass(frozen=True)
class JobCounts:
    """One job's counts.

    Attributes:
        job: Job name (``P2``).
        passed: Tests that passed.
        failed: Tests that failed.
        skipped: Tests skipped by a harness verdict (downgrade, dependency, condition, abort).
        without_result: Selected tests that wrote no result (``JOB_RESULT_MISSING``).
        not_passed: The results that did not pass, in run order, the missing ones last.
    """

    job: str
    passed: int
    failed: int
    skipped: int
    without_result: int
    not_passed: tuple[TestOutcome, ...]

    @property
    def selected(self) -> int:
        """Every test the job selected."""
        return self.passed + self.failed + self.skipped + self.without_result

    def line(self) -> str:
        """The counts as one log line.

        Returns:
            e.g. ``P2 job: 406 selected, 400 passed, 4 failed, 2 skipped, 0 without a result``.
        """
        return (f"{self.job} job: {self.selected} selected, {self.passed} passed, {self.failed} failed, "
                f"{self.skipped} skipped, {self.without_result} without a result")


def job_counts(job: str, outcomes: Iterable[TestOutcome], missing: Iterable[TestOutcome]) -> JobCounts:
    """Count one job's results.

    Args:
        job: Job name.
        outcomes: The job's results (a test written twice counts once, its last result).
        missing: Selected tests without a result (``gate.missing_results``).

    Returns:
        The counts.
    """
    results = list({outcome.nodeid: outcome for outcome in outcomes}.values())
    absent = list(missing)
    return JobCounts(
        job=job,
        passed=sum(1 for outcome in results if outcome.status is Status.PASSED),
        failed=sum(1 for outcome in results if outcome.status is Status.FAILED),
        skipped=sum(1 for outcome in results if outcome.status is Status.SKIPPED),
        without_result=len(absent),
        not_passed=tuple([outcome for outcome in results if not outcome.passed] + absent))


def system_label(environ: Mapping[str, str]) -> str:
    """The operating system to name in a summary title.

    Args:
        environ: Environment variables (``RUNNER_OS`` in GitHub Actions).

    Returns:
        ``" (Linux)"``, ``" (Windows)"`` or ``""`` outside GitHub Actions.
    """
    system = environ.get("RUNNER_OS", "")
    return f" ({system})" if system else ""


def _cell(text: str) -> str:
    """Text safe inside one Markdown table cell."""
    flat = " ".join(text.split()).replace("|", "\\|")
    return flat if len(flat) <= _MESSAGE_LIMIT else flat[:_MESSAGE_LIMIT - 3] + "..."


def counts_markdown(counts: JobCounts, environ: Mapping[str, str]) -> str:
    """The step summary section for one job.

    Args:
        counts: The job's counts.
        environ: Environment variables (for the operating system).

    Returns:
        Markdown: a title, the counts table and up to ``LISTED_LIMIT`` tests that did not pass.
    """
    lines = [f"### {counts.job} job{system_label(environ)}", "",
             "| Selected | Passed | Failed | Skipped | Without a result |", "|---|---|---|---|---|",
             f"| {counts.selected} | {counts.passed} | {counts.failed} | {counts.skipped} | "
             f"{counts.without_result} |", ""]
    if counts.not_passed:
        lines.extend(["| Test | Status | Keyword | Step | Message |", "|---|---|---|---|---|"])
        for outcome in counts.not_passed[:LISTED_LIMIT]:
            lines.append(f"| {_cell(outcome.case_id or outcome.nodeid)} | {outcome.status} | "
                         f"{_cell(outcome.primary)} | {_cell(outcome.step)} | {_cell(outcome.message)} |")
        rest = len(counts.not_passed) - LISTED_LIMIT
        if rest > 0:
            lines.append(f"\n{rest} more not passed; see `report.html` in the job's reports.")
        lines.append("")
    return "\n".join(lines) + "\n"


def append_step_summary(markdown: str, environ: Mapping[str, str]) -> None:
    """Append Markdown to the GitHub step summary when there is one; never fail the job over it.

    Args:
        markdown: What to append.
        environ: Environment variables (``GITHUB_STEP_SUMMARY`` names the file).
    """
    target = environ.get(_SUMMARY_VARIABLE, "")
    if not target:
        return
    try:
        with Path(target).open("a", encoding="utf-8") as handle:
            handle.write(markdown)
    except OSError as error:
        print(f"warning: step summary not written ({error})", file=sys.stderr)


def report_job(counts: JobCounts, environ: Mapping[str, str] | None = None) -> None:
    """Print the counts line and, in GitHub Actions, append the job's section to the step summary.

    Args:
        counts: The job's counts.
        environ: Environment variables (``os.environ`` by default).
    """
    variables = os.environ if environ is None else environ
    print(counts.line())
    append_step_summary(counts_markdown(counts, variables), variables)

# SPDX-FileCopyrightText: 2026 Aleksandr Polskiy
# SPDX-License-Identifier: Apache-2.0
"""``python -m emi_harness.gate <run dir>...``: the quality gates over a run's results (design §9.7).

Exit 0 when every gate holds, 1 otherwise, 2 when there are no results or any result comes from a run with
a named fault (``--emi-fault``, design §13.4). A test a job selected but wrote no result for counts as not
passed (``JOB_RESULT_MISSING``). ``--expect P2`` names a job that was chosen to run: a directory without its
``collection.json`` (the job never started, or failed before selecting tests, so its tests are not even
known) fails the gate with ``JOB_RESULT_MISSING``. Each directory is checked on its own, so CI passes one
directory per operating system. In GitHub Actions the report is also appended to the step summary (AMB-72).
"""

import argparse
import json
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from emi_harness.keywords import JobKeyword
from emi_harness.outcomes import Status, TestOutcome
from emi_harness.priorities import Job, evaluate_gates, job_name
from emi_harness.results import load_results
from emi_harness.summary import append_step_summary

_COLLECTION_FILE = "collection.json"


def main(argv: Sequence[str] | None = None) -> int:
    """Print the gate report.

    Args:
        argv: Report directories (``reports/<run>`` or one job's directory); several for several OSes.

    Returns:
        The exit code.
    """
    parser = argparse.ArgumentParser(prog="python -m emi_harness.gate", description="EMI quality gates")
    parser.add_argument("directories", nargs="+", type=Path, help="run or job report directories")
    parser.add_argument("--expect", action="append", default=[], type=job_name,
                        choices=[str(job) for job in Job], help="a job chosen to run (any case); repeatable")
    args = parser.parse_args(argv)
    outcomes = [outcome for directory in args.directories for outcome in load_results(directory)]
    absent = absent_jobs(args.directories, args.expect)
    for line in absent:
        print(f"{JobKeyword.RESULT_MISSING}: {line}", file=sys.stderr)
    if not outcomes:
        print("no results found", file=sys.stderr)
        return 2
    faults = sorted({outcome.fault for outcome in outcomes if outcome.fault})
    if faults:
        print(f"refused: results of a fault run ({', '.join(faults)}) prove that tests can fail; they are never "
              "gate results (design §13.4)", file=sys.stderr)
        return 2
    missing = [outcome for directory in args.directories for outcome in missing_results(directory)]
    for outcome in missing:
        print(f"{JobKeyword.RESULT_MISSING}: {outcome.case_id or outcome.nodeid} ({outcome.priority}) was selected "
              f"by job {outcome.job} but has no result", file=sys.stderr)
    report = evaluate_gates([*outcomes, *missing])
    print(report.render())
    if absent:
        print(f"gate failed: {len(absent)} chosen job(s) left no record of their tests", file=sys.stderr)
    holds = report.holds and not absent
    warnings = [report.time_share_warning] if report.time_share_warning else []
    append_step_summary(_gate_markdown(report.render(), absent, warnings, holds), os.environ)
    return 0 if holds else 1


def _gate_markdown(rendered: str, absent: Sequence[str], warnings: Sequence[str], holds: bool) -> str:
    """The gate's step summary section: verdict, absent jobs, warnings and the report as printed."""
    lines = [f"### Quality gate: {'PASS' if holds else 'FAIL'}", ""]
    lines.extend(f"* {JobKeyword.RESULT_MISSING}: {line}" for line in absent)
    lines.extend(f"* Warning: {warning}" for warning in warnings)
    lines.extend(["", "```", rendered, "```", ""])
    return "\n".join(lines) + "\n"


def absent_jobs(directories: Sequence[Path], expected: Sequence[str]) -> list[str]:
    """The chosen jobs that left no ``collection.json`` in a directory.

    Such a job never started or failed before selecting its tests (a usage error, a broken checkout), so
    not even the number of its tests is known; without this check the gate would measure the other jobs
    alone and could pass a run in which most tests never ran.

    Args:
        directories: Report directories, one per operating system.
        expected: Names of the jobs chosen to run.

    Returns:
        One line per job and directory without a collection.
    """
    return [f"job {job} left no {_COLLECTION_FILE} in {directory} (it never started or failed before selecting "
            "tests)" for directory in directories for job in absent_job_names(directory, expected)]


def absent_job_names(directory: Path, expected: Sequence[str]) -> list[str]:
    """The chosen jobs that left no ``collection.json`` in one directory.

    Args:
        directory: One report directory.
        expected: Names of the jobs chosen to run.

    Returns:
        The job names, in the order given.
    """
    collected = {path.parent.name for path in directory.rglob(_COLLECTION_FILE)}
    return [job for job in expected if job not in collected]


def missing_results(directory: Path) -> list[TestOutcome]:
    """A not-passed result for every test a job selected that has no result of its own.

    A job lists its selected tests in ``collection.json``; a test lost to a crashed worker or a killed
    runner would otherwise just be absent, and the gates would measure fewer tests than ran. Only the
    directory's own results count, so a result on one operating system does not cover the other.

    Args:
        directory: One report directory.

    Returns:
        One SKIPPED result with ``JOB_RESULT_MISSING`` per selected test without a result.
    """
    present = {outcome.nodeid for outcome in load_results(directory)}
    missing: dict[str, TestOutcome] = {}
    for path in sorted(directory.rglob(_COLLECTION_FILE)):
        selected = json.loads(path.read_text(encoding="utf-8")).get("selected", [])
        for entry in selected:
            if entry["nodeid"] not in present:
                missing[entry["nodeid"]] = TestOutcome(
                    nodeid=entry["nodeid"], case_id=entry["case_id"], priority=entry["priority"],
                    status=Status.SKIPPED, primary=str(JobKeyword.RESULT_MISSING),
                    message="selected but no result was written", job=path.parent.name)
    return list(missing.values())


if __name__ == "__main__":
    sys.exit(main())

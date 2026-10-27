# SPDX-FileCopyrightText: 2026 Aleksandr Polskiy
# SPDX-License-Identifier: Apache-2.0
"""``python -m emi_harness.gate <run dir>...``: the quality gates over a run's results (design §9.7).

Exit 0 when every gate holds, 1 otherwise, 2 when there are no results or any result comes from a run with
a named fault (``--emi-fault``, design §13.4). A test a job selected but wrote no result for counts as not
passed (``JOB_RESULT_MISSING``).
"""

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from emi_harness.keywords import JobKeyword
from emi_harness.outcomes import Status, TestOutcome
from emi_harness.priorities import evaluate_gates
from emi_harness.results import load_results

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
    args = parser.parse_args(argv)
    outcomes = [outcome for directory in args.directories for outcome in load_results(directory)]
    if not outcomes:
        print("no results found", file=sys.stderr)
        return 2
    faults = sorted({outcome.fault for outcome in outcomes if outcome.fault})
    if faults:
        print(f"refused: results of a fault run ({', '.join(faults)}) prove that tests can fail; they are never "
              "gate results (design §13.4)", file=sys.stderr)
        return 2
    missing = missing_results(args.directories, outcomes)
    for outcome in missing:
        print(f"{JobKeyword.RESULT_MISSING}: {outcome.case_id or outcome.nodeid} ({outcome.priority}) was selected "
              f"by job {outcome.job} but has no result", file=sys.stderr)
    report = evaluate_gates([*outcomes, *missing])
    print(report.render())
    return 0 if report.holds else 1


def missing_results(directories: Sequence[Path], outcomes: Sequence[TestOutcome]) -> list[TestOutcome]:
    """A not-passed result for every test a job selected that has no result of its own.

    A job lists its selected tests in ``collection.json``; a test lost to a crashed worker or a killed
    runner would otherwise just be absent, and the gates would measure fewer tests than ran.

    Args:
        directories: Report directories.
        outcomes: The results found.

    Returns:
        One SKIPPED result with ``JOB_RESULT_MISSING`` per selected test without a result.
    """
    present = {outcome.nodeid for outcome in outcomes}
    missing: dict[str, TestOutcome] = {}
    for directory in directories:
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

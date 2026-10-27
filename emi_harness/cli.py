# SPDX-FileCopyrightText: 2026 Aleksandr Polskiy
# SPDX-License-Identifier: Apache-2.0
"""``python -m emi_harness.cli``: run functional tests (design §9.1).

```
python -m emi_harness.cli                              # every functional test
python -m emi_harness.cli --test C400001               # one test (case ID, function name or node ID)
python -m emi_harness.cli --file smoke.txt             # one name per line
python -m emi_harness.cli --keyword READER_IMAP_       # tests tied to a keyword or prefix
python -m emi_harness.cli --priority P0 -n 4           # one job, 4 workers
```
Every ``--emi-*`` option is passed to pytest unchanged. Reports go to ``reports/<run>/<job>/`` (JUnit XML,
HTML, results, Allure results with ``--allure``).

Exit codes: 0 the job passed; 1 the job failed (P0/P1: any test not passed, a P0 failure also printed as
JOB_CRITICAL_FUNCTIONAL_FAILURE; P2-P4: under 85% passed; P2, P3, P4 alone: never, since their 85% holds
for the three together and ``emi_harness.gate`` measures it; debug/all: pytest's verdict); 2 the job stopped
early (abort: infrastructure, circuit breaker or time budget), or an earlier job of the run aborted;
4 usage error; 5 no tests selected. A failed test never stops a job: its dependents are skipped instead,
in this job and in later jobs of the same run (AMB-65).
"""

import argparse
import os
import shutil
import subprocess
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

import pytest

from emi_api import RunId
from emi_harness.jobs import read_stop
from emi_harness.keywords import JobKeyword
from emi_harness.options import add_arguments, cli_values, pass_through, read_dotenv, resolve
from emi_harness.priorities import LOW_GATE, Job
from emi_harness.results import job_dir, load_results

EXIT_PASSED, EXIT_FAILED, EXIT_ABORTED, EXIT_USAGE = 0, 1, 2, 4
_UPSTREAM = {Job.P1: (Job.P0,), **dict.fromkeys((Job.P2, Job.P3, Job.P4, Job.P2_P4), (Job.P0, Job.P1))}

PytestRunner = Callable[[list[str]], int]


def build_parser() -> argparse.ArgumentParser:
    """The CLI parser, with every option of the option table.

    Returns:
        The parser.
    """
    parser = argparse.ArgumentParser(prog="python -m emi_harness.cli", description="Run EMI functional tests.")
    parser.add_argument("paths", nargs="*", default=["tests"], help="test paths (default: tests)")
    parser.add_argument("--test", action="append", default=[], help="test to run; repeatable")
    parser.add_argument("--file", help="file with one test name per line")
    parser.add_argument("--keyword", action="append", default=[], help="keyword or prefix; repeatable")
    parser.add_argument("--priority", default=str(Job.ALL), type=_job_name, choices=[str(job) for job in Job],
                        help="job to run: P0, P1, P2, P3, P4, P2-P4, debug or all (any case)")
    parser.add_argument("--allure", action="store_true", help="also write Allure results (and HTML with the CLI)")
    parser.add_argument("--calibrate", action="store_true", help="record durations and suggest time limits")
    parser.add_argument("-n", "--workers", type=int, default=0, help="parallel workers (pytest-xdist)")
    add_arguments(parser.add_argument_group("settings (also EMI_* variables or .env)"))
    return parser


def _job_name(value: str) -> str:
    """A job name in its canonical case (``p3`` -> ``P3``, ``ALL`` -> ``all``); unknown names are kept for argparse."""
    return next((str(job) for job in Job if str(job).lower() == value.strip().lower()), value)


def _upstream_abort(reports: Path, run_id: str, job: Job) -> str:
    """Name an earlier job of this run that aborted (the cascade stops lower jobs)."""
    for earlier in _UPSTREAM.get(job, ()):
        decision = read_stop(job_dir(reports, run_id, str(earlier)))
        if decision is not None:
            return f"{earlier} job aborted ({decision.primary}: {decision.message})"
    return ""


def _job_exit(directory: Path, job: Job, pytest_code: int) -> int:
    """The job's exit code from its results."""
    if read_stop(directory) is not None:
        return EXIT_ABORTED
    if pytest_code not in (0, 1):
        return pytest_code
    results = {outcome.nodeid: outcome for outcome in load_results(directory)}.values()
    failed = [outcome.case_id or outcome.nodeid for outcome in results if not outcome.passed]
    if job is Job.P0 and failed:
        print(f"{JobKeyword.CRITICAL_FUNCTIONAL_FAILURE}: P0 test(s) not passed: {', '.join(failed)} "
              "(the job finished; only their dependents were skipped)", file=sys.stderr)
    if job in (Job.P0, Job.P1):
        return EXIT_PASSED if all(outcome.passed for outcome in results) else EXIT_FAILED
    if job.low:
        passed = sum(1 for outcome in results if outcome.passed)
        if job is Job.P2_P4:
            return EXIT_PASSED if not results or passed / len(results) >= LOW_GATE else EXIT_FAILED
        # One of P2, P3, P4: the 85% holds for the three together, so quality-gate decides it (AMB-67).
        print(f"{job} job: {passed} of {len(results)} passed; P2-P4 together must reach {LOW_GATE:.0%}, "
              "measured by the quality gate over all three jobs", file=sys.stderr)
        return EXIT_PASSED
    return pytest_code


def main(argv: Sequence[str] | None = None, runner: PytestRunner = pytest.main) -> int:
    """Run one job.

    Args:
        argv: Arguments (``sys.argv[1:]`` by default).
        runner: Runs pytest with arguments (replaced in unit tests).

    Returns:
        The exit code.
    """
    args = build_parser().parse_args(argv)
    try:
        resolved = resolve(cli_values(args), os.environ, read_dotenv())
        resolved.emi_settings()
    except ValueError as error:
        print(f"invalid option: {error}", file=sys.stderr)
        return EXIT_USAGE
    job = Job(args.priority)
    run_id = resolved["run_id"] or os.environ.get("EMI_RUN_ID", "") or str(RunId.new(0))
    os.environ["EMI_RUN_ID"] = run_id
    reports = Path(resolved["reports_dir"])
    upstream = _upstream_abort(reports, run_id, job)
    if upstream:
        print(f"{job} job not run: {upstream}", file=sys.stderr)
        return EXIT_ABORTED
    directory = job_dir(reports, run_id, str(job))
    directory.mkdir(parents=True, exist_ok=True)
    code = runner(_pytest_args(args, job, directory, resolved["max_connections"]))
    if args.allure:
        _allure_html(directory / "allure-results", directory / "allure-report")
    print(f"reports: {directory}")
    return _job_exit(directory, job, int(code))


def _pytest_args(args: argparse.Namespace, job: Job, directory: Path, max_connections: int) -> list[str]:
    """pytest's arguments: paths, job, selections, settings, reports and workers (capped at the budget)."""
    pytest_args = [*args.paths, "--emi-job", str(job), *pass_through(args),
                   "--junitxml", str(directory / "junit.xml"), "--html", str(directory / "report.html"),
                   "--self-contained-html"]
    for name in args.test:
        pytest_args.extend(("--emi-test", name))
    if args.file:
        pytest_args.extend(("--emi-test-file", args.file))
    for keyword in args.keyword:
        pytest_args.extend(("--emi-keyword", keyword))
    if args.calibrate:
        pytest_args.append("--emi-calibrate")
    workers = args.workers
    if workers > max_connections:
        print(f"warning: -n {workers} lowered to max_connections={max_connections} "
              "(one IMAP connection per worker)", file=sys.stderr)
        workers = max_connections
    if workers > 0:
        pytest_args.extend(("-n", str(workers), "--dist", "loadgroup"))
    if args.allure:
        pytest_args.extend(("--alluredir", str(directory / "allure-results")))
    return pytest_args


def _allure_html(results: Path, target: Path) -> None:
    """Build the Allure HTML report when the Allure CLI is installed; warn otherwise (AMB-03)."""
    allure = shutil.which("allure")
    if allure is None:
        print(f"warning: Allure CLI not found; Allure results kept in {results}, no Allure HTML built "
              "(JUnit XML and HTML reports are complete)", file=sys.stderr)
        return
    subprocess.run([allure, "generate", str(results), "-o", str(target), "--clean"], check=False)


if __name__ == "__main__":
    sys.exit(main())

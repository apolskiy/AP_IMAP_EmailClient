# SPDX-FileCopyrightText: 2026 Aleksandr Polskiy
# SPDX-License-Identifier: Apache-2.0
"""``python -m emi_harness.validity``: do the functional tests test what they claim? (design §13.4, AMB-64)

```
python -m emi_harness.validity faults                         # every named fault against its catching tests
python -m emi_harness.validity faults --fault reader-wrong-link
python -m emi_harness.validity stability --repeat 3 -n 4      # repeat runs: flaky tests, thin time margins
python -m emi_harness.validity stability --test C401000 --priority all
```
``faults`` runs each fault's catching tests (listed with the suite, ``tests/functional/validity.py``) under
that fault; every one must fail with an expected keyword, otherwise it is a **gap** (a false positive the
suite would let through). ``stability`` repeats a selection on a healthy setup; a test that does not pass in
every repeat is **flaky** or **failing** (a false negative), and a test or step close to its time limit is a
**thin margin**. Each run is an ordinary harness job with its own run ID; the summary goes to
``reports/validity/<stamp>/``.

Exit codes: 0 nothing found; 1 gaps or findings; 2 a run aborted or produced no results; 4 usage error.
"""

import argparse
import importlib
import json
import os
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from emi_api import RunId
from emi_harness.faults import FAULTS
from emi_harness.outcomes import Status, TestOutcome
from emi_harness.results import job_dir, load_results

EXIT_CLEAN, EXIT_FOUND, EXIT_NO_RESULTS, EXIT_USAGE = 0, 1, 2, 4
_ABORTED = 2
_DEFAULT_CATCHES = "tests.functional.validity"

# Runs one harness CLI job: (CLI arguments, extra environment) -> exit code.
JobRunner = Callable[[list[str], Mapping[str, str]], int]


@dataclass(frozen=True)
class Catch:
    """Tests that must catch a fault, and the keywords they may fail with.

    Attributes:
        cases: TestRail case IDs.
        keywords: Primary keywords that count as catching the fault.
    """

    cases: tuple[str, ...]
    keywords: tuple[str, ...]


@dataclass(frozen=True)
class CatchResult:
    """How one catching test did under its fault.

    Attributes:
        fault: The fault.
        case_id: The test.
        expected: Keywords that count as caught.
        status: ``passed``, ``failed``, ``skipped`` or ``not run``.
        primary: The keyword it ended with.
        caught: True when it failed with an expected keyword.
    """

    fault: str
    case_id: str
    expected: tuple[str, ...]
    status: str
    primary: str
    caught: bool


@dataclass(frozen=True)
class Finding:
    """One stability finding.

    Attributes:
        case_id: The test.
        kind: ``flaky`` (passed in some repeats), ``failing`` (in none) or ``thin margin``.
        detail: Statuses and keywords per repeat, or the duration against the limit.
    """

    case_id: str
    kind: str
    detail: str


def evaluate_catches(fault: str, catches: Sequence[Catch], outcomes: Sequence[TestOutcome]) -> list[CatchResult]:
    """Check that every catching test failed with an expected keyword.

    Args:
        fault: The fault the run used.
        catches: Its catching tests.
        outcomes: The run's results.

    Returns:
        One result per catching test, in catalog order.
    """
    by_case = {outcome.case_id: outcome for outcome in outcomes}
    results: list[CatchResult] = []
    for catch in catches:
        for case_id in catch.cases:
            outcome = by_case.get(case_id)
            status = str(outcome.status) if outcome else "not run"
            primary = outcome.primary if outcome else ""
            caught = outcome is not None and outcome.status is Status.FAILED and primary in catch.keywords
            results.append(CatchResult(fault, case_id, catch.keywords, status, primary, caught))
    return results


def analyse_stability(repeats: Sequence[Sequence[TestOutcome]], margin: float) -> list[Finding]:
    """Find flaky and failing tests and thin time margins over repeated runs.

    Args:
        repeats: Each repeat's results.
        margin: Share of a limit above which a duration is a thin margin (0.8 = 80%).

    Returns:
        The findings, by case ID.
    """
    runs: dict[str, list[TestOutcome | None]] = {}
    for index, outcomes in enumerate(repeats):
        for outcome in outcomes:
            runs.setdefault(outcome.case_id or outcome.nodeid, [None] * len(repeats))[index] = outcome
    findings: list[Finding] = []
    for case_id, history in sorted(runs.items()):
        passed = sum(1 for outcome in history if outcome is not None and outcome.passed)
        if passed < len(history):
            detail = ", ".join("not run" if outcome is None else
                               f"{outcome.status}{' ' + outcome.primary if outcome.primary else ''}"
                               for outcome in history)
            findings.append(Finding(case_id, "flaky" if passed else "failing", detail))
        findings.extend(_margins(case_id, [outcome for outcome in history if outcome is not None], margin))
    return findings


def _margins(case_id: str, outcomes: Sequence[TestOutcome], margin: float) -> list[Finding]:
    """A test or step whose longest duration is above ``margin`` of its limit."""
    found: list[Finding] = []
    longest = max(outcomes, key=lambda outcome: outcome.duration, default=None)
    if longest is not None and longest.limit > 0 and longest.duration > margin * longest.limit:
        found.append(Finding(case_id, "thin margin", f"test took {longest.duration:.1f} s of {longest.limit:.1f} s"))
    steps: dict[str, tuple[float, float]] = {}
    for outcome in outcomes:
        for record in outcome.steps:
            seen = steps.get(record.label, (0.0, record.limit))
            steps[record.label] = (max(seen[0], record.duration), record.limit or seen[1])
    for label, (duration, limit) in sorted(steps.items()):
        if limit > 0 and duration > margin * limit:
            found.append(Finding(case_id, "thin margin", f"step {label} took {duration:.1f} s of {limit:.1f} s"))
    return found


def _subprocess_runner(arguments: list[str], extra: Mapping[str, str]) -> int:
    """Run one harness job in its own process (pytest is best started once per process)."""
    return subprocess.run([sys.executable, "-m", "emi_harness.cli", *arguments], env={**os.environ, **extra},
                          check=False).returncode


def _job(runner: JobRunner, reports: Path, arguments: list[str], extra: Mapping[str, str],
         job: str) -> tuple[int, list[TestOutcome]]:
    """Run one job with a new run ID; its exit code and results."""
    run_id = str(RunId.new(0))
    code = runner(arguments, {**extra, "EMI_RUN_ID": run_id})
    return code, load_results(job_dir(reports, run_id, job))


def build_parser() -> argparse.ArgumentParser:
    """The command's parser.

    Returns:
        The parser.
    """
    parser = argparse.ArgumentParser(prog="python -m emi_harness.validity", description="EMI test validity checks")
    commands = parser.add_subparsers(dest="command", required=True)
    faults = commands.add_parser("faults", help="prove each named fault is caught by its tests")
    faults.add_argument("--fault", action="append", default=[], choices=sorted(FAULTS), help="fault; repeatable")
    faults.add_argument("--catches", default=_DEFAULT_CATCHES, help="module with the CATCHES table")
    faults.add_argument("--wait-timeout", type=float, default=15.0,
                        help="seconds a wait may take in fault runs (a missing email is expected there)")
    stability = commands.add_parser("stability", help="repeat a selection on a healthy setup")
    stability.add_argument("--repeat", type=int, default=3, help="number of repeats (default 3)")
    stability.add_argument("--margin", type=float, default=0.8, help="thin-margin share of a limit (default 0.8)")
    stability.add_argument("--priority", default="all", help="job: P0, P1, P2, P3, P4, P2-P4, debug or all")
    stability.add_argument("--test", action="append", default=[], help="test to run; repeatable")
    stability.add_argument("--file", help="file with one test name per line")
    stability.add_argument("--keyword", action="append", default=[], help="keyword or prefix; repeatable")
    for command in (faults, stability):
        command.add_argument("-n", "--workers", type=int, default=0, help="parallel workers")
        command.add_argument("paths", nargs="*", default=["tests"], help="test paths (default: tests)")
    return parser


def main(argv: Sequence[str] | None = None, runner: JobRunner = _subprocess_runner) -> int:
    """Run the chosen check.

    Args:
        argv: Arguments (``sys.argv[1:]`` by default).
        runner: Runs one harness job (replaced in unit tests).

    Returns:
        The exit code.
    """
    args = build_parser().parse_args(argv)
    reports = Path(os.environ.get("EMI_REPORTS_DIR", "reports"))
    summary = reports / "validity" / datetime.now(timezone.utc).strftime("%Y%m%dt%H%M%S")
    if args.command == "faults":
        return _faults(args, runner, reports, summary)
    return _stability(args, runner, reports, summary)


def _load_table(module: str, chosen: Sequence[str]) -> Mapping[str, Sequence[Catch]] | None:
    """The CATCHES table, or ``None`` (with the reason printed) when it is missing or lacks a chosen fault."""
    try:
        table: Mapping[str, Sequence[Catch]] = importlib.import_module(module).CATCHES
    except (ImportError, AttributeError) as error:
        print(f"cannot load the CATCHES table from {module}: {error}", file=sys.stderr)
        return None
    missing = [name for name in chosen if name not in table]
    if missing:
        print(f"no catching tests listed for: {', '.join(missing)}", file=sys.stderr)
        return None
    return table


def _run_fault(args: argparse.Namespace, runner: JobRunner, reports: Path, name: str,
               catches: Sequence[Catch]) -> tuple[list[CatchResult], str]:
    """One fault against its catching tests: the results and a problem ("" when the run produced results)."""
    arguments = [*args.paths, "--priority", "all", "--emi-fault", name,
                 "--emi-wait-timeout", str(args.wait_timeout), "-n", str(args.workers)]
    for case_id in (case_id for catch in catches for case_id in catch.cases):
        arguments.extend(("--test", case_id))
    code, outcomes = _job(runner, reports, arguments, {}, "all")
    healthy = code != _ABORTED and bool(outcomes)
    problem = "" if healthy else f"{name}: the run aborted or produced no results (exit {code})"
    return evaluate_catches(name, catches, outcomes), problem


def _faults(args: argparse.Namespace, runner: JobRunner, reports: Path, summary: Path) -> int:
    """Every chosen fault against its catching tests."""
    table = _load_table(args.catches, args.fault)
    if table is None:
        return EXIT_USAGE
    chosen = args.fault or sorted(table)
    results: list[CatchResult] = []
    problems: list[str] = []
    for name in chosen:
        own, problem = _run_fault(args, runner, reports, name, table[name])
        results.extend(own)
        problems.extend([problem] if problem else [])
        print(f"{name}: {sum(result.caught for result in own)} of {len(own)} catching tests caught it")
    gaps = [result for result in results if not result.caught]
    _write(summary / "faults.json", {"results": [asdict(result) for result in results], "problems": problems})
    for gap in gaps:
        print(f"GAP {gap.fault}: {gap.case_id} {gap.status} {gap.primary or '-'} "
              f"(expected failed with {' or '.join(gap.expected)})")
    for problem in problems:
        print(f"PROBLEM {problem}", file=sys.stderr)
    print(f"summary: {summary / 'faults.json'}")
    return EXIT_NO_RESULTS if problems else (EXIT_FOUND if gaps else EXIT_CLEAN)


def _stability(args: argparse.Namespace, runner: JobRunner, reports: Path, summary: Path) -> int:
    """Repeat the selection and report flaky and failing tests and thin margins."""
    if args.repeat < 2:
        print("--repeat must be at least 2", file=sys.stderr)
        return EXIT_USAGE
    arguments = [*args.paths, "--priority", args.priority, "-n", str(args.workers)]
    for name in args.test:
        arguments.extend(("--test", name))
    if args.file:
        arguments.extend(("--file", args.file))
    for keyword in args.keyword:
        arguments.extend(("--keyword", keyword))
    repeats: list[list[TestOutcome]] = []
    for _repeat in range(args.repeat):
        code, outcomes = _job(runner, reports, arguments, {}, args.priority)
        if code == _ABORTED or not outcomes:
            print(f"a repeat aborted or produced no results (exit {code})", file=sys.stderr)
            return EXIT_NO_RESULTS
        repeats.append(outcomes)
    findings = analyse_stability(repeats, args.margin)
    _write(summary / "stability.json", {"repeats": args.repeat, "tests": len({outcome.case_id for outcome in
                                                                               repeats[0]}),
                                        "findings": [asdict(finding) for finding in findings]})
    for finding in findings:
        print(f"{finding.kind.upper()} {finding.case_id}: {finding.detail}")
    print(f"{len(findings)} finding(s) over {args.repeat} repeats; summary: {summary / 'stability.json'}")
    return EXIT_FOUND if findings else EXIT_CLEAN


def _write(path: Path, data: object) -> None:
    """Write a JSON summary."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


if __name__ == "__main__":
    sys.exit(main())

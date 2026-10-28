# SPDX-FileCopyrightText: 2026 Aleksandr Polskiy
# SPDX-License-Identifier: Apache-2.0
"""``python -m emi_harness.allure_merge <out> --os ubuntu=<dir> --os windows=<dir> --expect P0 ...`` (design §9.8).

Builds the one Allure results folder the published report is generated from. Every job of every operating
system writes its own ``allure-results``, and Allure shows only the results it is given, so on their own
they hide what did not run. The merge makes the report show every test that should have run, once per
operating system:

* **Each system separately.** Every result gets an ``OS`` parameter and a history ID of its own per system.
  Otherwise Allure takes the Ubuntu and Windows results of a test for retries of one test and shows only
  the later one, so a failure on one system could hide behind a pass on the other.
* **Tests a job selected but wrote no result for** (a crashed worker, a killed runner) are added as broken
  with ``[JOB_RESULT_MISSING]``, as the gate counts them (``gate.missing_results``).
* **Jobs that never ran their tests.** A chosen job that left no ``collection.json`` never started or failed
  before selecting its tests, so it wrote no results at all. Its tests are listed with
  ``pytest --collect-only`` (no mail server needed) and each is added as broken with
  ``[JOB_RESULT_MISSING]``, so the report's totals include them.
"""

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from emi_harness.allure_report import priority_labels, write_categories
from emi_harness.gate import absent_job_names, missing_results
from emi_harness.keywords import JobKeyword
from emi_harness.priorities import Job, job_name

_RESULT_SUFFIX = "-result.json"
_COLLECTION_FILE = "collection.json"

Collector = Callable[[str], list[dict[str, str]]]


def main(argv: Sequence[str] | None = None, collector: Collector | None = None) -> int:
    """Merge every operating system's Allure results into one folder.

    Args:
        argv: Arguments (``sys.argv[1:]`` by default).
        collector: Lists a job's tests (replaced in unit tests); ``pytest --collect-only`` by default.

    Returns:
        The exit code: 0, or 2 when no Allure result at all was written or added.
    """
    parser = argparse.ArgumentParser(prog="python -m emi_harness.allure_merge",
                                     description="Merge Allure results of several operating systems")
    parser.add_argument("output", type=Path, help="the merged allure-results folder")
    parser.add_argument("--os", action="append", default=[], type=_system, dest="systems", required=True,
                        metavar="LABEL=DIR", help="a system's label and report directory; repeatable")
    parser.add_argument("--expect", action="append", default=[], type=job_name,
                        choices=[str(job) for job in Job], help="a job chosen to run (any case); repeatable")
    parser.add_argument("--tests", action="append", default=[], help="test paths to collect (default: tests)")
    args = parser.parse_args(argv)
    paths = args.tests or ["tests"]
    listed: dict[str, list[dict[str, str]]] = {}

    def tests_of(job: str) -> list[dict[str, str]]:
        """A job's tests, listed once for every system."""
        if job not in listed:
            listed[job] = collector(job) if collector else collect_job(job, paths)
        return listed[job]

    args.output.mkdir(parents=True, exist_ok=True)
    write_categories(args.output)
    total = sum(merge_system(args.output, label, directory, args.expect, tests_of)
                for label, directory in args.systems)
    if not total:
        print("no Allure results", file=sys.stderr)
        return 2
    return 0


def merge_system(output: Path, label: str, directory: Path, expected: Sequence[str], tests_of: Collector) -> int:
    """Merge one system's results and add a broken result for each of its tests that has none.

    Args:
        output: The merged folder.
        label: The system's label, e.g. ``ubuntu``.
        directory: The system's report directory.
        expected: Names of the jobs chosen to run.
        tests_of: Lists a job's tests.

    Returns:
        The number of results copied or added.
    """
    copied = copy_results(directory, output, label)
    missing = missing_results(directory)
    for outcome in missing:
        entry = {"nodeid": outcome.nodeid, "case_id": outcome.case_id, "priority": outcome.priority}
        write_not_run(output, entry, label, f"[{JobKeyword.RESULT_MISSING}] selected by job {outcome.job} "
                      f"({label}) but no result was written")
    absent = absent_job_names(directory, expected)
    not_run = [entry for job in absent for entry in tests_of(job)]
    for job in absent:
        for entry in tests_of(job):
            write_not_run(output, entry, label, f"[{JobKeyword.RESULT_MISSING}] job {job} ({label}) did not run "
                          "its tests: it never started or failed before selecting them")
    jobs = f" ({', '.join(absent)})" if absent else ""
    print(f"{label}: {copied} result(s); {len(missing)} selected without a result; {len(not_run)} in job(s) that "
          f"did not run{jobs}")
    return copied + len(missing) + len(not_run)


def _system(text: str) -> tuple[str, Path]:
    """``LABEL=DIR`` as (label, directory)."""
    label, separator, directory = text.partition("=")
    if not separator or not label or not directory:
        raise argparse.ArgumentTypeError(f"{text!r} is not LABEL=DIR")
    return label, Path(directory)


def copy_results(directory: Path, output: Path, label: str) -> int:
    """Copy every Allure file of one system's jobs, giving each result the system as a parameter.

    Args:
        directory: One system's report directory (``allure-results`` folders anywhere below it).
        output: The merged folder.
        label: The system's label, e.g. ``ubuntu``.

    Returns:
        The number of results copied.
    """
    copied = 0
    for results_dir in sorted(directory.rglob("allure-results")):
        for path in sorted(results_dir.iterdir()):
            if path.name.endswith(_RESULT_SUFFIX):
                result = json.loads(path.read_text(encoding="utf-8"))
                (output / path.name).write_text(json.dumps(for_system(result, label)), encoding="utf-8")
                copied += 1
            elif path.is_file():
                shutil.copy2(path, output / path.name)
    return copied


def for_system(result: dict[str, Any], label: str) -> dict[str, Any]:
    """A result marked as one system's: an ``OS`` parameter and a history ID of its own.

    Args:
        result: An allure-pytest result.
        label: The system's label.

    Returns:
        The changed result (the same dictionary).
    """
    result["parameters"] = [*result.get("parameters", []), {"name": "OS", "value": label}]
    result["historyId"] = _digest(f"{result.get('historyId') or result.get('fullName', '')}:{label}")
    return result


def write_not_run(output: Path, entry: Mapping[str, str], label: str, message: str) -> None:
    """Write a broken result for a test that should have run on this system but has no result.

    Args:
        output: The merged folder.
        entry: The test as ``collection.json`` lists it: ``nodeid``, ``case_id``, ``priority``.
        label: The system's label.
        message: ``[KEYWORD] message``, which also files it under Allure's "Job stopped" category.
    """
    path, rest = entry["nodeid"].split("::", 1)
    parts = rest.split("::")
    module = path.removesuffix(".py").replace("/", ".")
    function = parts[-1].split("[", 1)[0]
    container = f"{module}.{parts[0]}" if len(parts) > 1 else module
    labels = [*priority_labels(entry["priority"]), ("suite", Path(path).stem), ("host", label)]
    if len(parts) > 1:
        labels.append(("subSuite", parts[0]))
    now = int(time.time() * 1000)
    result_id = str(uuid.uuid4())
    result = {
        "name": parts[-1], "status": "broken", "statusDetails": {"message": message},
        "start": now, "stop": now, "uuid": result_id,
        "historyId": _digest(f"{entry['nodeid']}:{label}"), "testCaseId": _digest(entry["nodeid"]),
        "fullName": f"{container}#{function}",
        "labels": [{"name": name, "value": value} for name, value in labels],
        "parameters": [{"name": "OS", "value": label}],
    }
    (output / f"{result_id}{_RESULT_SUFFIX}").write_text(json.dumps(result), encoding="utf-8")


def collect_job(job: str, paths: Sequence[str]) -> list[dict[str, str]]:
    """The tests a job selects, from ``pytest --collect-only`` with the harness plugin.

    Collection needs no mail server or broker; the plugin writes ``collection.json`` as a real job does.

    Args:
        job: The job's name, e.g. ``P1``.
        paths: Test paths.

    Returns:
        The selected tests as ``collection.json`` lists them; empty when collection failed.
    """
    with tempfile.TemporaryDirectory() as scratch:
        completed = subprocess.run([sys.executable, "-m", "pytest", *paths, "--collect-only", "-q", "-p",
                                    "no:cacheprovider", "--emi-job", job, "--emi-reports-dir", scratch],
                                   capture_output=True, text=True, check=False)
        files = sorted(Path(scratch).rglob(_COLLECTION_FILE))
        if not files:
            print(f"could not list job {job}'s tests (pytest exit {completed.returncode}): "
                  f"{completed.stdout[-500:]}{completed.stderr[-500:]}", file=sys.stderr)
            return []
        return list(json.loads(files[0].read_text(encoding="utf-8")).get("selected", []))


def _digest(text: str) -> str:
    """An MD5 hex digest, as allure-pytest's history and test case IDs are."""
    return hashlib.md5(text.encode("utf-8"), usedforsecurity=False).hexdigest()


if __name__ == "__main__":
    sys.exit(main())

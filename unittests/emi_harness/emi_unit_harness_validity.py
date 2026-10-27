# SPDX-FileCopyrightText: 2026 Aleksandr Polskiy
# SPDX-License-Identifier: Apache-2.0
"""Unit tests: the test-validity checks, with the harness jobs replaced (design §13.4)."""

from collections.abc import Callable, Mapping
from pathlib import Path

import pytest

from emi_harness import Status, TestOutcome
from emi_harness.faults import FAULTS
from emi_harness.outcomes import StepRecord
from emi_harness.results import ResultsWriter, job_dir
from emi_harness.validity import (
    EXIT_CLEAN,
    EXIT_FOUND,
    EXIT_NO_RESULTS,
    EXIT_USAGE,
    Catch,
    analyse_stability,
    evaluate_catches,
    main,
    stability_jobs,
)
from tests.functional.validity import CATCHES

_FAILED = "CASE_VERIFICATION_FAILED"


def _outcome(case_id: str, status: Status = Status.PASSED, primary: str = "", *, duration: float = 1.0,
             limit: float = 90.0, steps: tuple[StepRecord, ...] = ()) -> TestOutcome:
    """A stored result."""
    return TestOutcome(nodeid=f"tests/emi_func_x.py::{case_id}", case_id=case_id, priority="P2", status=status,
                       primary=primary, duration=duration, limit=limit, steps=steps, job="all")


def _runner(reports: Path, results: Callable[[int], list[TestOutcome]], calls: list[list[str]],
            job: str = "all") -> Callable[[list[str], Mapping[str, str]], int]:
    """A job runner that stores scripted results under the run ID it is given."""
    def run(arguments: list[str], extra: Mapping[str, str]) -> int:
        calls.append(arguments)
        writer = ResultsWriter(job_dir(reports, extra["EMI_RUN_ID"], job), "main")
        for outcome in results(len(calls)):
            writer.write(outcome)
        return 0
    return run


@pytest.fixture(name="reports")
def _reports(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Reports under tmp."""
    monkeypatch.setenv("EMI_REPORTS_DIR", str(tmp_path / "reports"))
    return tmp_path / "reports"


class TestEMIHarnessValidity:
    """Named faults must be caught; repeats must agree; limits must keep a margin."""

    def EMI_HARNESS_UNIT_200140_evaluate_catches(self) -> None:
        """Caught only when failed with an expected keyword; a pass, another keyword or a missing test is a gap."""
        catches = (Catch(("C1", "C2", "C3", "C4"), ("CASE_EMAIL_NOT_RECEIVED", _FAILED)),)
        outcomes = [_outcome("C1", Status.FAILED, _FAILED), _outcome("C2"),
                    _outcome("C3", Status.FAILED, "CASE_UNEXPECTED_ERROR")]
        results = {result.case_id: result for result in evaluate_catches("fault", catches, outcomes)}
        assert [results[case].caught for case in ("C1", "C2", "C3", "C4")] == [True, False, False, False]
        assert (results["C2"].status, results["C4"].status) == ("passed", "not run")

    def EMI_HARNESS_UNIT_200141_analyse_stability(self) -> None:
        """Flaky (passed sometimes), failing (never), thin margins for a test and a step; steady tests are fine."""
        slow_step = StepRecord(label="S01", description="wait", status=Status.PASSED, duration=27.0, limit=30.0)
        first = [_outcome("C1"), _outcome("C2", Status.FAILED, "CASE_X"), _outcome("C3", Status.FAILED, "CASE_X"),
                 _outcome("C4", duration=80.0), _outcome("C5", steps=(slow_step,))]
        second = [_outcome("C1"), _outcome("C2"), _outcome("C3", Status.SKIPPED, "CASE_Y"),
                  _outcome("C4", duration=10.0), _outcome("C5")]
        findings = {(finding.case_id, finding.kind): finding for finding in analyse_stability([first, second], 0.8)}
        assert set(findings) == {("C2", "flaky"), ("C3", "failing"), ("C4", "thin margin"), ("C5", "thin margin")}
        assert findings[("C2", "flaky")].detail == "failed CASE_X, passed"
        assert "80.0 s of 90.0 s" in findings[("C4", "thin margin")].detail
        assert "step S01 took 27.0 s of 30.0 s" in findings[("C5", "thin margin")].detail

    def EMI_HARNESS_UNIT_200142_faults_caught(self, reports: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """Every catching test failed as expected: exit 0; the run used the fault and selected exactly them."""
        cases = CATCHES["reader-ignores-markers"][0].cases
        calls: list[list[str]] = []
        runner = _runner(reports, lambda _call: [_outcome(case, Status.FAILED, _FAILED) for case in cases], calls)
        assert main(["faults", "--fault", "reader-ignores-markers"], runner=runner) == EXIT_CLEAN
        arguments = calls[0]
        assert arguments[arguments.index("--emi-fault") + 1] == "reader-ignores-markers"
        assert [arguments[index + 1] for index, word in enumerate(arguments) if word == "--test"] == list(cases)
        assert "3 of 3 catching tests caught it" in capsys.readouterr().out
        assert list((reports / "validity").glob("*/faults.json"))

    def EMI_HARNESS_UNIT_200143_faults_gap(self, reports: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """A catching test that passed under its fault is a gap: exit 1, named with its status."""
        cases = CATCHES["reader-ignores-markers"][0].cases
        calls: list[list[str]] = []
        runner = _runner(reports, lambda _call: [_outcome(cases[0]), *(_outcome(case, Status.FAILED, _FAILED)
                                                                       for case in cases[1:])], calls)
        assert main(["faults", "--fault", "reader-ignores-markers"], runner=runner) == EXIT_FOUND
        assert f"GAP reader-ignores-markers: {cases[0]} passed" in capsys.readouterr().out

    def EMI_HARNESS_UNIT_200144_faults_without_results(self, reports: Path) -> None:
        """A run that produced nothing is not a clean result: exit 2."""
        calls: list[list[str]] = []
        runner = _runner(reports, lambda _call: [], calls)
        assert main(["faults", "--fault", "reader-wrong-link"], runner=runner) == EXIT_NO_RESULTS

    def EMI_HARNESS_UNIT_200145_stability(self, reports: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """Repeats with their own run IDs; a test that failed once is flaky (exit 1); steady runs exit 0."""
        calls: list[list[str]] = []
        flaky = _runner(reports, lambda call: [_outcome("C1", Status.FAILED if call == 2 else Status.PASSED,
                                                        "CASE_X" if call == 2 else "")], calls)
        assert main(["stability", "--repeat", "3", "--test", "C1"], runner=flaky) == EXIT_FOUND
        assert len(calls) == 3 and "FLAKY C1: passed, failed CASE_X, passed" in capsys.readouterr().out
        steady_calls: list[list[str]] = []
        steady = _runner(reports, lambda _call: [_outcome("C1")], steady_calls)
        assert main(["stability", "--repeat", "2", "--test", "C1"], runner=steady) == EXIT_CLEAN
        assert main(["stability", "--repeat", "1"], runner=steady) == EXIT_USAGE

    def EMI_HARNESS_UNIT_200165_stability_runs_the_chain_and_deletes_mail(self, reports: Path) -> None:
        """Every priority: P0 to P4 under one run ID per repeat, each job deleting its run's mail; an abort
        ends the repeat as it ends the regression chain; a narrowed selection stays one job (AMB-79)."""
        calls: list[tuple[list[str], str]] = []

        def chain(arguments: list[str], extra: Mapping[str, str]) -> int:
            job = arguments[arguments.index("--priority") + 1]
            calls.append((arguments, extra["EMI_RUN_ID"]))
            ResultsWriter(job_dir(reports, extra["EMI_RUN_ID"], job), "main").write(_outcome(f"C{job}"))
            return 2 if job == "P1" and len(calls) > 5 else 0

        assert main(["stability", "--repeat", "2"], runner=chain) == EXIT_NO_RESULTS
        jobs = [arguments[arguments.index("--priority") + 1] for arguments, _run in calls]
        assert jobs == ["P0", "P1", "P2", "P3", "P4", "P0", "P1"]  # the second repeat aborted in P1
        assert len({run for _arguments, run in calls[:5]}) == 1 and calls[5][1] != calls[0][1]
        assert all(arguments[-2:] == ["--emi-delete-own-mail", "on"] for arguments, _run in calls)
        assert stability_jobs("all", narrowed=True) == ("all",) and stability_jobs("P2", narrowed=False) == ("P2",)
        assert stability_jobs("ALL", narrowed=False) == ("P0", "P1", "P2", "P3", "P4")

    def EMI_HARNESS_UNIT_200146_catch_table_is_consistent(self) -> None:
        """Every fault has catching tests, every case ID is unique within its fault and is a functional ID."""
        assert set(CATCHES) == set(FAULTS)
        for name, catches in CATCHES.items():
            cases = [case for catch in catches for case in catch.cases]
            assert cases, name
            assert len(cases) == len(set(cases)), name
            assert all(400000 <= int(case[1:]) <= 599999 for case in cases), name

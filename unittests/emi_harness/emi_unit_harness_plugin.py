# SPDX-FileCopyrightText: 2026 Aleksandr Polskiy
# SPDX-License-Identifier: Apache-2.0
"""Unit tests: the pytest plugin inside a throwaway project, and the harness and gate CLIs (design §9)."""

from pathlib import Path

import pytest

from emi_harness import Status, TestOutcome
from emi_harness.cli import EXIT_ABORTED, EXIT_FAILED, EXIT_PASSED, EXIT_USAGE, main
from emi_harness.gate import main as gate_main
from emi_harness.jobs import JobController
from emi_harness.keywords import CaseKeyword, HarnessKeyword, JobKeyword
from emi_harness.priorities import Job
from emi_harness.results import ResultsWriter, job_dir, load_results
from emi_reader import ReaderKeyword

_RUN_ID = "20261026t153012-w00-a1b2"
_INI = """
[tool.pytest.ini_options]
python_files = ["emi_*.py"]
python_classes = ["TestEMI*"]
python_functions = ["EMI_*"]
"""
_HEADER = """
import time

import pytest

from emi_harness import CaseSkip
from emi_reader import ReaderKeyword


class TestEMIDemo:
"""


@pytest.fixture(name="project")
def _project(pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch) -> pytest.Pytester:
    """A throwaway project with the EMI naming rules and a fixed run ID."""
    monkeypatch.setenv("EMI_RUN_ID", _RUN_ID)
    monkeypatch.delenv("PYTEST_XDIST_WORKER", raising=False)
    pytester.makepyprojecttoml(_INI)
    return pytester


def _functional(project: pytest.Pytester, body: str) -> None:
    """Write one functional test module under tests/."""
    project.makepyfile(**{"tests/emi_func_demo": _HEADER + body})


def _run(project: pytest.Pytester, *args: str) -> tuple[pytest.RunResult, dict[str, TestOutcome]]:
    """Run pytest with the plugin; return the result and the stored outcomes by case ID."""
    reports = project.path / "reports"
    result = project.runpytest("-p", "emi_harness.plugin", "-p", "no:cacheprovider",
                               "--emi-reports-dir", str(reports), *args)
    return result, {outcome.case_id: outcome for outcome in load_results(reports)}


class TestEMIHarnessPlugin:
    """Outcomes, stop rules, dependencies, selection and duplicate IDs through pytest itself."""

    def EMI_HARNESS_UNIT_200100_keyword_outcomes(self, project: pytest.Pytester) -> None:
        """Plain assert fails with CASE_VERIFICATION_FAILED; an untyped error with CASE_UNEXPECTED_ERROR."""
        _functional(project, """
    @pytest.mark.emi(priority="P2")
    def EMI_DEMO_FUNC_400001_passes(self) -> None:
        assert True

    @pytest.mark.emi(priority="P2")
    def EMI_DEMO_FUNC_400002_asserts(self) -> None:
        assert 1 == 2, "values differ"

    @pytest.mark.emi(priority="P2")
    def EMI_DEMO_FUNC_400003_raises(self) -> None:
        raise KeyError("missing")
""")
        result, outcomes = _run(project)
        result.assert_outcomes(passed=1, failed=2)
        assert outcomes["C400001"].status is Status.PASSED
        assert outcomes["C400002"].primary == CaseKeyword.VERIFICATION_FAILED
        assert outcomes["C400003"].primary == CaseKeyword.UNEXPECTED_ERROR
        assert "KeyError" in outcomes["C400003"].secondary
        # The keyword summary writes the status in lower case; fnmatch ignores case only on Windows.
        result.stdout.fnmatch_lines(["*EMI job all*", f"*1  failed {CaseKeyword.VERIFICATION_FAILED}*"])

    def EMI_HARNESS_UNIT_200101_infrastructure_skip_stops_job(self, project: pytest.Pytester) -> None:
        """An infrastructure skip skips the test and every later one with the same keyword (fail fast)."""
        _functional(project, """
    @pytest.mark.emi(priority="P2")
    def EMI_DEMO_FUNC_400011_server_down(self) -> None:
        raise CaseSkip("no server", ReaderKeyword.IMAP_CONNECTION_REFUSED, step="S01", infrastructure=True)

    @pytest.mark.emi(priority="P2")
    def EMI_DEMO_FUNC_400012_later(self) -> None:
        assert True
""")
        result, outcomes = _run(project)
        result.assert_outcomes(skipped=2)
        assert outcomes["C400011"].infrastructure and outcomes["C400011"].step == "S01"
        assert outcomes["C400012"].primary == ReaderKeyword.IMAP_CONNECTION_REFUSED
        result.stdout.fnmatch_lines([f"*JOB STOPPED: {ReaderKeyword.IMAP_CONNECTION_REFUSED}*"])

    def EMI_HARNESS_UNIT_200109_p0_failure_skips_only_dependents(self, project: pytest.Pytester) -> None:
        """A failed P0 test does not stop the P0 job; in the later P1 job only its dependents are skipped."""
        _functional(project, """
    @pytest.mark.emi(priority="P0")
    def EMI_DEMO_FUNC_400061_parent(self) -> None:
        assert 1 == 2

    @pytest.mark.emi(priority="P0")
    def EMI_DEMO_FUNC_400062_other_p0(self) -> None:
        assert True

    @pytest.mark.emi(priority="P1", depends=["C400061"])
    def EMI_DEMO_FUNC_400063_child(self) -> None:
        assert True

    @pytest.mark.emi(priority="P1")
    def EMI_DEMO_FUNC_400064_independent(self) -> None:
        assert True

    @pytest.mark.emi(priority="P2")
    @pytest.mark.parametrize("number", [pytest.param(number, marks=pytest.mark.emi(case=f"C4001{number:02d}"))
                                        for number in range(16)])
    def EMI_DEMO_FUNC_400100_filler(self, number: int) -> None:
        assert number >= 0
""")
        first, _outcomes = _run(project, "--emi-job", "P0")
        first.assert_outcomes(passed=1, failed=1)
        second, outcomes = _run(project, "--emi-job", "P1")
        second.assert_outcomes(passed=1, skipped=1)
        assert outcomes["C400062"].status is Status.PASSED
        assert outcomes["C400063"].primary == CaseKeyword.DEPENDENCY_SKIPPED
        assert outcomes["C400064"].status is Status.PASSED

    def EMI_HARNESS_UNIT_200102_dependency_skip_and_order(self, project: pytest.Pytester) -> None:
        """A child written before its parent runs after it, and is skipped when the parent fails."""
        _functional(project, """
    @pytest.mark.emi(priority="P2", depends=["C400021"])
    def EMI_DEMO_FUNC_400022_child(self) -> None:
        assert True

    @pytest.mark.emi(priority="P2")
    def EMI_DEMO_FUNC_400021_parent(self) -> None:
        assert False
""")
        result, outcomes = _run(project, "-v")
        result.assert_outcomes(failed=1, skipped=1)
        result.stdout.fnmatch_lines(["*400021_parent FAILED*", "*400022_child SKIPPED*"])
        assert outcomes["C400022"].primary == CaseKeyword.DEPENDENCY_SKIPPED

    def EMI_HARNESS_UNIT_200103_test_time_limit(self, project: pytest.Pytester) -> None:
        """A test that passes but runs over its own limit fails with CASE_TIMEOUT."""
        _functional(project, """
    @pytest.mark.emi(priority="P2", timeout=0.01)
    def EMI_DEMO_FUNC_400031_slow(self) -> None:
        time.sleep(0.1)
""")
        result, outcomes = _run(project)
        result.assert_outcomes(failed=1)
        assert outcomes["C400031"].primary == CaseKeyword.TIMEOUT

    def EMI_HARNESS_UNIT_200104_job_and_name_selection(self, project: pytest.Pytester) -> None:
        """--emi-job keeps the job's priorities; --emi-test narrows by case ID."""
        _functional(project, """
    @pytest.mark.emi(priority="P0")
    def EMI_DEMO_FUNC_400041_critical(self) -> None:
        assert True

    @pytest.mark.emi(priority="P0")
    def EMI_DEMO_FUNC_400042_critical(self) -> None:
        assert True
""" + "".join(f"""
    def EMI_DEMO_FUNC_4001{number:02d}_standard(self) -> None:
        assert True
""" for number in range(10)))
        result, _outcomes = _run(project, "--emi-job", "P0")
        result.assert_outcomes(passed=2, deselected=10)
        result, _outcomes = _run(project, "--emi-job", "P0", "--emi-test", "C400042")
        result.assert_outcomes(passed=1, deselected=11)

    def EMI_HARNESS_UNIT_200105_duplicate_ids_refused(self, project: pytest.Pytester) -> None:
        """Two tests with one TestRail ID stop collection, naming both."""
        _functional(project, """
    def EMI_DEMO_FUNC_400051_first(self) -> None:
        assert True

    def EMI_DEMO_FUNC_400051_second(self) -> None:
        assert True
""")
        result, _outcomes = _run(project)
        assert result.ret == pytest.ExitCode.USAGE_ERROR
        result.stderr.fnmatch_lines([f"*{HarnessKeyword.DUPLICATE_TEST_ID}*", "*400051_first*400051_second*"])

    def EMI_HARNESS_UNIT_200107_parametrized_cases_own_ids(self, project: pytest.Pytester) -> None:
        """Each parametrized case takes its case ID from its own marker; function and class markers fill the
        rest (priority, depends), and a case marker overrides them."""
        _functional(project, """
    @pytest.mark.emi(priority="P3")
    @pytest.mark.parametrize("value", [
        pytest.param(1, marks=pytest.mark.emi(case="C401001")),
        pytest.param(2, marks=pytest.mark.emi(case="C401002", priority="P2")),
    ])
    def EMI_DEMO_FUNC_401000_family(self, value: int) -> None:
        assert value != 2
""")
        result, outcomes = _run(project, "--emi-test", "C401002")
        result.assert_outcomes(failed=1, deselected=1)
        assert outcomes["C401002"].priority == "P2"
        result, outcomes = _run(project)
        assert outcomes["C401001"].priority == "P3" and outcomes["C401001"].passed

    def EMI_HARNESS_UNIT_200108_parametrized_without_ids_refused(self, project: pytest.Pytester) -> None:
        """Cases that share the function's ID are duplicates: collection stops."""
        _functional(project, """
    @pytest.mark.parametrize("value", [1, 2])
    def EMI_DEMO_FUNC_401100_family(self, value: int) -> None:
        assert value
""")
        result, _outcomes = _run(project)
        assert result.ret == pytest.ExitCode.USAGE_ERROR
        result.stderr.fnmatch_lines([f"*{HarnessKeyword.DUPLICATE_TEST_ID}*", "*C401100*"])

    def EMI_HARNESS_UNIT_200106_unit_tests_untouched(self, project: pytest.Pytester) -> None:
        """Tests outside tests/ run as plain pytest: no results and no report folders."""
        project.makepyfile(**{"unit/emi_unit_demo": """
def EMI_DEMO_UNIT_200999_plain() -> None:
    assert True
"""})
        result, outcomes = _run(project)
        result.assert_outcomes(passed=1)
        assert not outcomes and not (project.path / "reports").exists()


def _outcome(case_id: str, priority: str, status: Status, job: str) -> TestOutcome:
    """A stored result."""
    return TestOutcome(nodeid=f"tests/emi_func_demo.py::{case_id}", case_id=case_id, priority=priority,
                       status=status, primary="" if status is Status.PASSED else "CASE_X", duration=1.0, job=job)


class TestEMIHarnessCli:
    """The harness CLI with pytest replaced (design §9.1)."""

    @pytest.fixture(name="reports")
    def _reports(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        """Fixed run ID, reports under tmp, no .env from the working directory."""
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("EMI_RUN_ID", _RUN_ID)
        monkeypatch.setenv("EMI_REPORTS_DIR", str(tmp_path / "reports"))
        return tmp_path / "reports"

    def EMI_HARNESS_UNIT_200110_arguments_passed(self, reports: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """Job, selections, report paths and options reach pytest; -n is capped at max_connections."""
        calls: list[list[str]] = []
        code = main(["--priority", "P1", "--test", "C400001", "--keyword", "READER_", "-n", "50",
                     "--emi-max-connections", "4"], runner=lambda args: calls.append(args) or 0)
        args = calls[0]
        directory = job_dir(reports, _RUN_ID, "P1")
        assert code == EXIT_PASSED and args[:3] == ["tests", "--emi-job", "P1"]
        assert ["--emi-max-connections", "4"] == args[3:5] and str(directory / "junit.xml") in args
        assert args[args.index("-n") + 1] == "4" and "loadgroup" in args
        assert args[args.index("--emi-test") + 1] == "C400001" and "READER_" in args
        assert "lowered to max_connections=4" in capsys.readouterr().err

    def EMI_HARNESS_UNIT_200111_usage_error(self, reports: Path) -> None:
        """An invalid setting stops before pytest runs."""
        del reports
        calls: list[list[str]] = []
        assert main(["--emi-wait-timeout", "never"], runner=lambda args: calls.append(args) or 0) == EXIT_USAGE
        assert not calls

    def EMI_HARNESS_UNIT_200112_upstream_abort(self, reports: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """When P0 aborted (infrastructure), P1 and P2-P4 do not run (exit 2)."""
        controller = JobController(Job.P0, stop_dir=job_dir(reports, _RUN_ID, "P0"))
        controller.record(TestOutcome(nodeid="tests/emi_func_demo.py::C400001", case_id="C400001", priority="P0",
                                      status=Status.SKIPPED, primary="GREENMAIL_NOT_READY", infrastructure=True))
        calls: list[list[str]] = []
        for job in ("P1", "P2-P4"):
            assert main(["--priority", job], runner=lambda args: calls.append(args) or 1) == EXIT_ABORTED
        assert not calls and "GREENMAIL_NOT_READY" in capsys.readouterr().err

    def EMI_HARNESS_UNIT_200115_p0_failure_reported(self, reports: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """A P0 failure fails the P0 job with JOB_CRITICAL_FUNCTIONAL_FAILURE, but P1 still runs (AMB-65)."""
        def p0_runner(args: list[str]) -> int:
            del args
            writer = ResultsWriter(job_dir(reports, _RUN_ID, "P0"), "main")
            writer.write(_outcome("C400001", "P0", Status.FAILED, "P0"))
            writer.write(_outcome("C400002", "P0", Status.PASSED, "P0"))
            return 1

        assert main(["--priority", "P0"], runner=p0_runner) == EXIT_FAILED
        error = capsys.readouterr().err
        assert f"{JobKeyword.CRITICAL_FUNCTIONAL_FAILURE}: P0 test(s) not passed: C400001 " in error
        calls: list[list[str]] = []
        assert main(["--priority", "P1"], runner=lambda args: calls.append(args) or 0) == EXIT_PASSED
        assert len(calls) == 1

    @pytest.mark.parametrize(("job", "failed", "expected"), [("P1", 0, EXIT_PASSED), ("P1", 1, EXIT_FAILED),
                                                             ("P2-P4", 1, EXIT_PASSED), ("P2-P4", 2, EXIT_FAILED)])
    def EMI_HARNESS_UNIT_200113_job_exit_from_results(self, reports: Path, job: str, failed: int,
                                                      expected: int) -> None:
        """P1 needs every test passed; P2-P4 needs 85% (9 of 10 holds, 8 of 10 does not)."""
        priority = "P1" if job == "P1" else "P2"

        def runner(args: list[str]) -> int:
            del args
            writer = ResultsWriter(job_dir(reports, _RUN_ID, job), "main")
            for number in range(10):
                status = Status.FAILED if number < failed else Status.PASSED
                writer.write(_outcome(f"C4002{number:02d}", priority, status, job))
            return 1 if failed else 0

        assert main(["--priority", job], runner=runner) == expected

    def EMI_HARNESS_UNIT_200114_gate(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """The gate CLI: 0 when every gate holds, 1 when one fails, 2 without results."""
        assert gate_main([str(tmp_path)]) == 2
        writer = ResultsWriter(tmp_path / "P0", "main")
        writer.write(_outcome("C400301", "P0", Status.PASSED, "P0"))
        assert gate_main([str(tmp_path)]) == 0
        writer.write(_outcome("C400302", "P1", Status.FAILED, "P1"))
        assert gate_main([str(tmp_path)]) == 1
        assert "P1 (100%)" in capsys.readouterr().out

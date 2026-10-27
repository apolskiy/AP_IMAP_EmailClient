# SPDX-FileCopyrightText: 2026 Aleksandr Polskiy
# SPDX-License-Identifier: Apache-2.0
"""Unit tests: job stop rules, priorities, gates, dependencies and selection (design §9.4-§9.7)."""

from pathlib import Path

import pytest

from emi_harness import CaseKeyword, CaseSkip, Job, JobKeyword, Status, TestOutcome
from emi_harness.dependencies import DependencyTracker, dependency_groups, order_tests
from emi_harness.jobs import BREAKER_LIMIT, JobController, read_stop
from emi_harness.priorities import Planned, evaluate_gates, parse_priority, plan_priorities
from emi_harness.selection import Candidate, case_id_for, duplicate_ids, read_test_file, select


def _outcome(number: int, status: Status = Status.PASSED, *, priority: str = "P2", primary: str = "",
             infrastructure: bool = False, duration: float = 1.0) -> TestOutcome:
    """A result for test C<number>."""
    return TestOutcome(nodeid=f"tests/emi_x.py::EMI_X_FUNC_{number}_case", case_id=f"C{number}", priority=priority,
                       status=status, primary=primary, infrastructure=infrastructure, duration=duration)


class TestEMIHarnessJobs:
    """When a job stops (design §9.4)."""

    def EMI_HARNESS_UNIT_200020_infrastructure_stops_any_job(self, tmp_path: Path) -> None:
        """An infrastructure skip stops even the P1 job; the decision is shared through the stop file."""
        controller = JobController(Job.P1, stop_dir=tmp_path)
        decision = controller.record(_outcome(400001, Status.SKIPPED, priority="P1",
                                              primary="GREENMAIL_NOT_READY", infrastructure=True))
        assert decision is not None and decision.primary == "GREENMAIL_NOT_READY"
        other_worker = JobController(Job.P1, stop_dir=tmp_path)
        assert other_worker.stopped() == read_stop(tmp_path) and read_stop(tmp_path) is not None

    def EMI_HARNESS_UNIT_200021_p0_failure_does_not_stop(self) -> None:
        """P0 failures never stop the P0 job (AMB-65): the other P0 tests still run."""
        controller = JobController(Job.P0)
        for number in range(400001, 400011):
            assert controller.record(_outcome(number, Status.FAILED, priority="P0", primary="CASE_X")) is None
        assert controller.stopped() is None

    def EMI_HARNESS_UNIT_200026_breaker_off_for_fault_runs(self) -> None:
        """With the breaker turned off (a fault run), failures in a row never stop the job."""
        controller = JobController(Job.ALL, breaker=False)
        for number in range(BREAKER_LIMIT * 2):
            assert controller.record(_outcome(400001 + number, Status.FAILED, primary="CASE_A")) is None

    def EMI_HARNESS_UNIT_200022_p1_failures_isolated(self) -> None:
        """P1 failures never stop the P1 job (no circuit breaker there)."""
        controller = JobController(Job.P1)
        for number in range(400001, 400011):
            assert controller.record(_outcome(number, Status.FAILED, priority="P1", primary="CASE_X")) is None

    def EMI_HARNESS_UNIT_200023_circuit_breaker(self) -> None:
        """5 failures in a row with one keyword trip the breaker; a pass or another keyword resets it."""
        controller = JobController(Job.P2_P4)
        for number in range(BREAKER_LIMIT - 1):
            assert controller.record(_outcome(400001 + number, Status.FAILED, primary="CASE_A")) is None
        assert controller.record(_outcome(400010, Status.FAILED, primary="CASE_B")) is None
        assert controller.record(_outcome(400011, Status.PASSED)) is None
        assert controller.record(_outcome(400012, Status.SKIPPED, primary=CaseKeyword.DEPENDENCY_SKIPPED)) is None
        decisions = [controller.record(_outcome(400020 + number, Status.FAILED, primary="CASE_A"))
                     for number in range(BREAKER_LIMIT)]
        assert decisions[:-1] == [None] * (BREAKER_LIMIT - 1)
        last = decisions[-1]
        assert last is not None and (last.primary, last.secondary) == (JobKeyword.CIRCUIT_BREAKER_OPEN, ("CASE_A",))

    def EMI_HARNESS_UNIT_200024_job_time_budget(self) -> None:
        """Past the budget the job stops with JOB_TIMEOUT."""
        controller = JobController(Job.DEBUG, budget_sec=900.0)
        assert controller.stopped(899.0) is None
        decision = controller.stopped(901.0)
        assert decision is not None and decision.primary == JobKeyword.TIMEOUT

    def EMI_HARNESS_UNIT_200025_first_decision_wins(self, tmp_path: Path) -> None:
        """After a stop, later results change nothing."""
        controller = JobController(Job.P2_P4, stop_dir=tmp_path)
        controller.record(_outcome(400001, Status.SKIPPED, primary="READER_IMAP_CONNECTION_REFUSED",
                                   infrastructure=True))
        assert controller.record(_outcome(400002, Status.FAILED, priority="P0", primary="CASE_X")) is None
        decision = controller.stopped()
        assert decision is not None and decision.primary == "READER_IMAP_CONNECTION_REFUSED"


class TestEMIHarnessPriorities:
    """The 20% rule, downgrades and gates (design §9.7)."""

    def EMI_HARNESS_UNIT_200030_parse_priority(self) -> None:
        """P0-P4 in any case; P2 by default; anything else refused."""
        assert (parse_priority("p1"), parse_priority(None)) == ("P1", "P2")
        with pytest.raises(ValueError):
            parse_priority("P5")

    def EMI_HARNESS_UNIT_200027_jobs_and_their_priorities(self) -> None:
        """P2, P3 and P4 each run one priority with the breaker; P2-P4 runs all three; P0 and P1 have no breaker."""
        assert [Job(name).priorities for name in ("P2", "P3", "P4")] == [("P2",), ("P3",), ("P4",)]
        assert Job.P2_P4.priorities == ("P2", "P3", "P4") and Job.P1.priorities == ("P1",)
        assert all(Job(name).breaker and Job(name).low for name in ("P2", "P3", "P4", "P2-P4"))
        assert not any(job.breaker or job.low for job in (Job.P0, Job.P1))

    def EMI_HARNESS_UNIT_200031_downgrades_excess_p1(self) -> None:
        """Excess P1 tests move to P2: lowest rank first, then the highest test number."""
        tests = [Planned(f"n{number}", f"C{400000 + number}", "P2") for number in range(14)]
        tests += [Planned("p0", "C400100", "P0"), Planned("a", "C400101", "P1", rank=5),
                  Planned("b", "C400102", "P1"), Planned("c", "C400103", "P1"), Planned("d", "C400104", "P1"),
                  Planned("e", "C400105", "P1", rank=5)]
        plan = plan_priorities(tests)
        assert plan.allowed == 4 and [move.case_id for move in plan.downgrades] == ["C400104", "C400103"]
        assert plan.priorities["d"] == plan.priorities["c"] == "P2"
        assert plan.priorities["a"] == plan.priorities["e"] == plan.priorities["b"] == "P1"
        assert JobKeyword.PRIORITY_DOWNGRADED in plan.message()

    def EMI_HARNESS_UNIT_200032_p0_overflow(self) -> None:
        """P0 alone above 20% is JOB_PRIORITY_P0_OVERFLOW; nothing is moved."""
        tests = [Planned(f"n{number}", f"C{400000 + number}", "P0" if number < 3 else "P2") for number in range(10)]
        plan = plan_priorities(tests)
        assert plan.p0_overflow and not plan.downgrades and JobKeyword.PRIORITY_P0_OVERFLOW in plan.message()

    def EMI_HARNESS_UNIT_200033_gates_pass(self) -> None:
        """All P0/P1 pass, 19 of 20 low pass: every gate holds."""
        results = [_outcome(400001, priority="P0"), _outcome(400002, priority="P1")]
        results += [_outcome(400100 + number) for number in range(19)]
        results.append(_outcome(400200, Status.FAILED, primary="CASE_X"))
        report = evaluate_gates(results)
        assert report.holds and len(report.checks) == 4 and report.keywords == {"CASE_X": 1}
        assert "Result: PASS" in report.render()

    def EMI_HARNESS_UNIT_200034_skip_is_not_a_pass(self) -> None:
        """A skipped P1 test fails the P1 gate."""
        report = evaluate_gates([_outcome(400001, Status.SKIPPED, priority="P1", primary="GREENMAIL_NOT_READY")])
        assert not report.holds and not report.checks[1].holds

    def EMI_HARNESS_UNIT_200035_low_gate_stops_measuring(self) -> None:
        """Under 85% in P2-P4 the overall gate is not measured."""
        results = [_outcome(400100 + number, Status.FAILED if number < 3 else Status.PASSED, primary="CASE_X")
                   for number in range(10)]
        report = evaluate_gates(results)
        assert not report.holds and [check.name for check in report.checks][-1].startswith("P2-P4")

    def EMI_HARNESS_UNIT_200036_effective_floor(self) -> None:
        """With 30 P0/P1 and 70 low tests, at most 5 may fail, so P2-P4 needs about 92.9%."""
        results = [_outcome(400000 + number, priority="P1") for number in range(30)]
        results += [_outcome(401000 + number) for number in range(70)]
        assert evaluate_gates(results).effective_low_floor == pytest.approx(65 / 70)

    def EMI_HARNESS_UNIT_200037_last_result_per_test_wins(self) -> None:
        """A rerun's result replaces the earlier one."""
        report = evaluate_gates([_outcome(400001, Status.FAILED, priority="P0", primary="CASE_X"),
                                 _outcome(400001, priority="P0")])
        assert report.holds


class TestEMIHarnessDependencies:
    """Dependency skipping (design §9.5)."""

    def EMI_HARNESS_UNIT_200040_failed_parent_skips_chain(self) -> None:
        """A failed parent skips its child and grandchild, naming the top of the chain and its keyword."""
        tracker = DependencyTracker({"C2": ("C1",), "C3": ("C2",)})
        tracker.record(TestOutcome(nodeid="n1", case_id="C1", priority="P2", status=Status.FAILED,
                                   primary="CASE_LINK_NOT_FOUND"))
        with pytest.raises(CaseSkip) as caught:
            tracker.check("C2")
        tracker.record(TestOutcome(nodeid="n2", case_id="C2", priority="P2", status=Status.SKIPPED,
                                   primary=CaseKeyword.DEPENDENCY_SKIPPED))
        with pytest.raises(CaseSkip) as grandchild:
            tracker.check("C3")
        for skip in (caught.value, grandchild.value):
            assert skip.primary == CaseKeyword.DEPENDENCY_SKIPPED and "C1" in str(skip)
            assert skip.secondary == ("CASE_LINK_NOT_FOUND",)

    def EMI_HARNESS_UNIT_200041_passed_or_missing_parent_runs(self) -> None:
        """A passed parent, or one not run at all, lets the child run."""
        tracker = DependencyTracker({"C2": ("C1",), "C4": ("C9",)},
                                    earlier=[TestOutcome(nodeid="n1", case_id="C1", priority="P0",
                                                         status=Status.PASSED)])
        tracker.check("C2")
        tracker.check("C4")

    def EMI_HARNESS_UNIT_200042_order_and_cycles(self) -> None:
        """Parents come first; a cycle is refused."""
        assert order_tests({"C3": ("C1",), "C1": ()}, ["C3", "C2", "C1"]) == ["C1", "C3", "C2"]
        with pytest.raises(ValueError):
            order_tests({"C1": ("C2",), "C2": ("C1",)})

    def EMI_HARNESS_UNIT_200043_groups_join_chains(self) -> None:
        """Tests linked through any parent share one xdist group."""
        groups = dependency_groups({"C3": ("C1", "C2"), "C5": ("C4",)})
        assert groups["C1"] == groups["C2"] == groups["C3"] != groups["C5"] == groups["C4"]


class TestEMIHarnessSelection:
    """Case IDs and --test / --file / --keyword selection (design §9.1)."""

    def EMI_HARNESS_UNIT_200050_case_id(self) -> None:
        """From the name, or declared on the marker; invalid declarations refused."""
        assert case_id_for("EMI_TOOL_FUNC_400001_waits") == "C400001"
        assert case_id_for("EMI_TEST_BROKER_UNIT_130001_x") == "C130001"
        assert case_id_for("helper") == "" and case_id_for("anything", "400777") == "C400777"
        with pytest.raises(ValueError):
            case_id_for("x", "T1")

    def EMI_HARNESS_UNIT_200051_select_by_name(self) -> None:
        """Case ID, function name or node-ID part; unknown names are reported."""
        tests = [Candidate("tests/a.py::EMI_A_FUNC_400001_x", "EMI_A_FUNC_400001_x", "C400001"),
                 Candidate("tests/b.py::EMI_B_FUNC_400002_y", "EMI_B_FUNC_400002_y", "C400002")]
        chosen, unmatched = select(tests, names=["400001", "EMI_B_FUNC_400002_y", "C499999"])
        assert [item.case_id for item in chosen] == ["C400001", "C400002"] and unmatched == ["C499999"]
        assert [item.case_id for item in select(tests, names=["tests/b.py"])[0]] == ["C400002"]

    def EMI_HARNESS_UNIT_200052_select_by_keyword(self) -> None:
        """Declared keywords and the last result's keywords, by full name or prefix."""
        tests = [Candidate("n1", "a", "C400001", ("READER_IMAP_READ_TIMEOUT",)), Candidate("n2", "b", "C400002"),
                 Candidate("n3", "c", "C400003")]
        history = {"n2": TestOutcome(nodeid="n2", case_id="C400002", priority="P2", status=Status.FAILED,
                                     primary="CASE_LINK_NOT_FOUND", secondary=("READER_PARSE_NO_HTML_PART",))}
        assert [item.nodeid for item in select(tests, keywords=["reader_"], history=history)[0]] == ["n1", "n2"]
        assert [item.nodeid for item in select(tests, keywords=["CASE_LINK_NOT_FOUND"], history=history)[0]] == ["n2"]

    def EMI_HARNESS_UNIT_200053_test_file(self, tmp_path: Path) -> None:
        """One name per line; blank lines and comments ignored."""
        path = tmp_path / "list.txt"
        path.write_text("# smoke\nC400001\n\nEMI_B_FUNC_400002_y  # second\n", encoding="utf-8")
        assert read_test_file(path) == ["C400001", "EMI_B_FUNC_400002_y"]

    def EMI_HARNESS_UNIT_200054_duplicate_ids(self) -> None:
        """Two owners of one ID are reported; one owner seen twice is not."""
        found = duplicate_ids([("C1", "a"), ("C1", "a"), ("C2", "b"), ("C2", "c"), ("", "d"), ("", "e")])
        assert found == {"C2": ["b", "c"]}

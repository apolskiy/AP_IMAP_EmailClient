# SPDX-FileCopyrightText: 2026 Aleksandr Polskiy
# SPDX-License-Identifier: Apache-2.0
"""pytest plugin: options, collection rules, fixtures, outcomes and results (design §9).

Registered by the root ``conftest.py``. Only **functional** tests (under ``tests/``) get priorities, job
selection, session setup, keyword outcomes and results files; unit tests keep plain pytest behaviour apart
from the duplicate-ID check.

Fixtures: ``api`` (the session's :class:`emi_api.EmiApi`, after session setup), ``steps``
(:class:`emi_harness.steps.Steps`) and ``emi_run`` (``api.start_run`` with the test's case ID).
Marker: ``@pytest.mark.emi(priority="P1", rank=0, depends=["C400001"], timeout=90, keywords=[...],
case="C400001")``.
"""

import json
import os
import time
from collections.abc import Generator, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from emi_api import EmiApi, EmiRun, RunId, worker_number
from emi_harness.dependencies import DependencyTracker, dependency_groups, order_tests
from emi_harness.evidence import Attacher, Evidence, write_settings
from emi_harness.jobs import JobController, StopDecision
from emi_harness.keywords import CaseKeyword, HarnessKeyword
from emi_harness.options import Resolved, add_pytest_options, cli_values, read_dotenv, resolve
from emi_harness.outcomes import CaseSkip, Status, TestOutcome, Verdict, classify
from emi_harness.priorities import Job, Planned, parse_priority, plan_priorities
from emi_harness.results import ResultsWriter, calibrate, job_dir, latest_run, load_results
from emi_harness.selection import Candidate, case_id_for, duplicate_ids, read_test_file, select
from emi_harness.session import session_setup
from emi_harness.steps import StepHook, Steps

_JOB_BUDGETS: dict[Job, float | None] = {Job.P0: 300.0, Job.P1: 600.0, Job.P2_P4: 3600.0, Job.DEBUG: 900.0,
                                          Job.ALL: None}
_COLLECTION_FILE = "collection.json"


@dataclass
class ItemInfo:  # pylint: disable=too-many-instance-attributes
    """What the harness knows about one functional test while it runs."""

    case_id: str
    priority: str
    timeout: float | None = None
    started: float = 0.0
    verdict: Verdict | None = None
    step: str = ""
    steps: Steps | None = None
    cleanup_keywords: tuple[str, ...] = ()
    evidence: Evidence = field(default_factory=Evidence)


@dataclass
class HarnessState:  # pylint: disable=too-many-instance-attributes
    """Per-process harness state, kept on the pytest config."""

    resolved: Resolved | None
    error: str
    job: Job
    run_id: str
    directory: Path
    controller: JobController
    writer: ResultsWriter
    started: float = field(default_factory=time.monotonic)
    tracker: DependencyTracker | None = None
    hook: StepHook | None = None
    attacher: Attacher | None = None
    current: ItemInfo | None = None


_STATE = pytest.StashKey[HarnessState]()
_ITEM = pytest.StashKey[ItemInfo]()


# --- options and configuration ---------------------------------------------------------------------------

def pytest_addoption(parser: pytest.Parser) -> None:
    """Every ``--emi-*`` option from the option table, plus the run-selection options.

    Args:
        parser: pytest's parser.
    """
    group = parser.getgroup("emi", "EMI harness")
    add_pytest_options(group.addoption)
    group.addoption("--emi-job", dest="emi_job", default=None, choices=[str(job) for job in Job],
                    help="job to run: P0, P1, P2-P4, debug or all (default all)")
    group.addoption("--emi-test", dest="emi_test", action="append", default=[],
                    help="run this test (function name, case ID or node ID); repeatable")
    group.addoption("--emi-test-file", dest="emi_test_file", default=None,
                    help="file with one test name per line")
    group.addoption("--emi-keyword", dest="emi_keyword", action="append", default=[],
                    help="run tests tied to this keyword or keyword prefix; repeatable")
    group.addoption("--emi-calibrate", dest="emi_calibrate", action="store_true",
                    help="record p50/p95/p99 durations and suggest time limits")


def pytest_configure(config: pytest.Config) -> None:
    """Resolve options, the run ID and the job; prepare results and the stop rules.

    Args:
        config: pytest's config.
    """
    config.addinivalue_line("markers", "emi(priority, rank, depends, timeout, keywords, case): EMI test metadata")
    resolved, error = None, ""
    try:
        resolved = resolve(cli_values(config.option), os.environ, read_dotenv(Path(config.rootpath) / ".env"))
        resolved.emi_settings()
    except ValueError as problem:
        resolved, error = None, str(problem)
    base = (resolved["run_id"] if resolved else "") or os.environ.get("EMI_RUN_ID", "") or str(RunId.new(0))
    os.environ["EMI_RUN_ID"] = base  # pytest-xdist workers inherit it, so every worker shares the run
    try:
        run_id = str(RunId.parse(base).for_worker(worker_number()))
    except ValueError:
        run_id = base
    job = Job(config.option.emi_job or Job.ALL)
    reports = Path(resolved["reports_dir"]) if resolved else Path("reports")
    directory = job_dir(reports, run_id, str(job))
    budget = resolved["job_timeout"] if resolved and resolved["job_timeout"] > 0 else _JOB_BUDGETS[job]
    worker = os.environ.get("PYTEST_XDIST_WORKER", "main")
    hook, attacher = _allure(config)
    state = HarnessState(resolved=resolved, error=error, job=job, run_id=run_id, directory=directory,
                         controller=JobController(job, budget_sec=budget, stop_dir=directory),
                         writer=ResultsWriter(directory, worker), hook=hook, attacher=attacher)
    config.stash[_STATE] = state


def _allure(config: pytest.Config) -> tuple[StepHook | None, Attacher | None]:
    """``allure.step`` and a JSON ``allure.attach`` when Allure results are being written."""
    if not getattr(config.option, "allure_report_dir", None):
        return None, None
    try:
        import allure  # pylint: disable=import-outside-toplevel
    except ImportError:
        return None, None

    def _attach(name: str, text: str) -> None:
        allure.attach(text, name=name, attachment_type=allure.attachment_type.JSON)
    return allure.step, _attach


def _functional(item: pytest.Item) -> bool:
    """Whether the item is a functional test (under ``tests/``)."""
    try:
        relative = Path(item.path).resolve().relative_to(Path(item.config.rootpath).resolve())
    except ValueError:
        return False
    return relative.parts[:1] == ("tests",)


def _marker(item: pytest.Item) -> dict[str, Any]:
    """The ``emi`` markers' keyword arguments, merged: a parametrized case's marker wins over the function's,
    which wins over the class's (so each ``pytest.param`` can carry its own ``case`` ID)."""
    merged: dict[str, Any] = {}
    for marker in reversed(list(item.iter_markers("emi"))):
        merged.update(marker.kwargs)
    for marker in getattr(getattr(item, "callspec", None), "marks", ()):
        if marker.name == "emi":
            merged.update(marker.kwargs)
    return merged


# --- collection ------------------------------------------------------------------------------------------

def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Case IDs, duplicate check, priorities and downgrades, job and name/keyword selection, dependency order.

    Args:
        config: pytest's config.
        items: Collected items, changed in place.

    Raises:
        pytest.UsageError: For invalid options, duplicate IDs, P0 overflow or bad markers.
    """
    state = config.stash[_STATE]
    functional = [item for item in items if _functional(item)]
    if functional and state.resolved is None:
        raise pytest.UsageError(f"invalid EMI option: {state.error}")
    try:
        cases = {item.nodeid: case_id_for(getattr(item, "originalname", item.name), _marker(item).get("case"))
                 for item in items}
        priorities = {item.nodeid: parse_priority(_marker(item).get("priority")) for item in functional}
    except ValueError as error:
        raise pytest.UsageError(str(error)) from error
    _check_duplicates(items, cases, set(id(item) for item in functional))
    plan = plan_priorities([Planned(item.nodeid, cases[item.nodeid], priorities[item.nodeid],
                                    int(_marker(item).get("rank", 0))) for item in functional])
    message = plan.message()
    if plan.p0_overflow:
        raise pytest.UsageError(message)
    depends = {cases[item.nodeid]: tuple(case_id_for("", str(parent)) for parent in _marker(item).get("depends", ()))
               for item in functional if cases[item.nodeid] and _marker(item).get("depends")}
    for item in functional:
        item.stash[_ITEM] = ItemInfo(case_id=cases[item.nodeid], priority=plan.priorities[item.nodeid],
                                     timeout=_marker(item).get("timeout"), evidence=Evidence(state.attacher))
    kept, unmatched = _select(config, state, items, cases)
    deselected = [item for item in items if id(item) not in {id(chosen) for chosen in kept}]
    if deselected:
        config.hook.pytest_deselected(items=deselected)
    items[:] = _ordered(kept, cases, depends)
    _group_dependents(items, cases, depends)
    state.tracker = DependencyTracker(depends, [outcome for outcome in load_results(state.directory.parent)
                                                if outcome.job != str(state.job)])
    if functional and state.resolved is not None:
        write_settings(state.directory, state.resolved.describe())
        (state.directory / _COLLECTION_FILE).write_text(json.dumps({"priorities": message, "unmatched": unmatched},
                                                                   indent=2), encoding="utf-8")


def _group_dependents(items: list[pytest.Item], cases: dict[str, str], depends: dict[str, tuple[str, ...]]) -> None:
    """Keep a parent and its children on one pytest-xdist worker (``--dist loadgroup``)."""
    groups = dependency_groups(depends)
    for item in items:
        group = groups.get(cases[item.nodeid])
        if group:
            item.add_marker(pytest.mark.xdist_group(name=group))


def _check_duplicates(items: list[pytest.Item], cases: dict[str, str], functional: set[int]) -> None:
    """Refuse two tests with one TestRail ID (functional: per case; unit: per function)."""
    owners = [(cases[item.nodeid], item.nodeid if id(item) in functional else item.nodeid.split("[", 1)[0])
              for item in items]
    duplicates = duplicate_ids(owners)
    if duplicates:
        lines = [f"{case_id}: {', '.join(names)}" for case_id, names in sorted(duplicates.items())]
        raise pytest.UsageError(f"{HarnessKeyword.DUPLICATE_TEST_ID}: test IDs must be unique\n" + "\n".join(lines))


def _select(config: pytest.Config, state: HarnessState, items: list[pytest.Item],
            cases: dict[str, str]) -> tuple[list[pytest.Item], list[str]]:
    """Apply the job's priorities and ``--emi-test`` / ``--emi-test-file`` / ``--emi-keyword``."""
    names = list(config.option.emi_test)
    if config.option.emi_test_file:
        names.extend(read_test_file(Path(config.option.emi_test_file)))
    keywords = list(config.option.emi_keyword)
    allowed = state.job.priorities
    in_job = [item for item in items if _ITEM not in item.stash or item.stash[_ITEM].priority in allowed]
    if not names and not keywords:
        return in_job, []
    history: dict[str, TestOutcome] = {}
    if keywords and state.resolved is not None:
        latest = latest_run(Path(state.resolved["reports_dir"]))
        history = {outcome.nodeid: outcome for outcome in load_results(latest)} if latest else {}
    candidates = [Candidate(item.nodeid, getattr(item, "originalname", item.name), cases[item.nodeid],
                            tuple(str(word) for word in _marker(item).get("keywords", ()))) for item in in_job]
    chosen, unmatched = select(candidates, names=names, keywords=keywords, history=history)
    wanted = {candidate.nodeid for candidate in chosen}
    return [item for item in in_job if item.nodeid in wanted], unmatched


def _ordered(items: list[pytest.Item], cases: dict[str, str], depends: dict[str, tuple[str, ...]]) -> list[pytest.Item]:
    """Parents before children; otherwise collection order."""
    if not depends:
        return items
    try:
        order = order_tests(depends, [cases[item.nodeid] for item in items if cases[item.nodeid]])
    except ValueError as error:
        raise pytest.UsageError(str(error)) from error
    position = {case_id: index for index, case_id in enumerate(order)}
    indexed = list(enumerate(items))
    indexed.sort(key=lambda pair: (position.get(cases[pair[1].nodeid], pair[0]), pair[0]))
    return [item for _index, item in indexed]


# --- fixtures --------------------------------------------------------------------------------------------

@pytest.fixture(scope="session", name="api")
def _api(pytestconfig: pytest.Config) -> Iterator[EmiApi]:
    """The session's API, after session setup (design §9.6); a failed setup skips every test that uses it."""
    state = pytestconfig.stash[_STATE]
    if state.resolved is None:
        raise pytest.UsageError(f"invalid EMI option: {state.error}")
    api = EmiApi(state.resolved.emi_settings(), run_id=state.run_id)
    api.observe(lambda name, data: state.current.evidence.add(name, data) if state.current else None)
    try:
        session_setup(api, probe=state.resolved["probe"], probe_timeout=state.resolved["probe_timeout"])
        yield api
    finally:
        api.close()


@pytest.fixture(name="steps")
def _steps(request: pytest.FixtureRequest) -> Iterator[Steps]:
    """The test's step log; cleanups run when the test ends."""
    state = request.config.stash[_STATE]
    limit = state.resolved["step_timeout"] if state.resolved else 30.0
    info = request.node.stash.get(_ITEM, None)
    steps = Steps(step_timeout=limit, hook=state.hook, evidence=info.evidence if info else None)
    if info is not None:
        info.steps = steps
    yield steps
    keywords = steps.run_cleanups()
    if _ITEM in request.node.stash:
        request.node.stash[_ITEM].cleanup_keywords = keywords


@pytest.fixture(name="emi_run")
def _emi_run(api: EmiApi, request: pytest.FixtureRequest) -> EmiRun:
    """``api.start_run`` with this test's case ID."""
    info = request.node.stash.get(_ITEM, None)
    case_id = info.case_id if info else case_id_for(request.node.originalname)
    return api.start_run(case_id)


# --- running ---------------------------------------------------------------------------------------------

@pytest.hookimpl(tryfirst=True)
def pytest_runtest_setup(item: pytest.Item) -> None:
    """Skip when the job has stopped or a parent failed or was skipped.

    Args:
        item: The test about to run.

    Raises:
        CaseSkip: Job stopped (job keyword) or ``CASE_DEPENDENCY_SKIPPED``.
    """
    if _ITEM not in item.stash:
        return
    state = item.config.stash[_STATE]
    item.stash[_ITEM].started = time.monotonic()
    state.current = item.stash[_ITEM]
    decision = state.controller.stopped(time.monotonic() - state.started)
    if decision is not None:
        raise CaseSkip(f"job stopped: {decision.message}", decision.primary, secondary=decision.secondary)
    if state.tracker is not None and item.stash[_ITEM].case_id:
        state.tracker.check(item.stash[_ITEM].case_id)


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo[None]) -> Generator[None, pytest.TestReport,
                                                                                          pytest.TestReport]:
    """Turn errors into keyword outcomes, enforce the test time limit, record the result after teardown.

    Args:
        item: The test.
        call: The phase that ended.

    Returns:
        The (possibly changed) report.
    """
    report = yield
    if _ITEM not in item.stash:
        return report
    info = item.stash[_ITEM]
    state = item.config.stash[_STATE]
    if call.excinfo is not None and call.when in ("setup", "call") and info.verdict is None:
        _apply_error(item, report, info, call.excinfo.value)
    elif call.when == "call" and report.passed:
        _apply_time_limit(report, info, state, call.duration)
    if call.when == "call" or (call.when == "setup" and not report.passed):
        report.sections.extend(info.evidence.sections())
    if call.when == "teardown":
        outcome = _finish(item, info, state)
        report.user_properties = list(report.user_properties) + _properties(outcome)
    return report


def _apply_error(item: pytest.Item, report: pytest.TestReport, info: ItemInfo, error: BaseException) -> None:
    """Classify an error raised in setup or the test body."""
    if isinstance(error, pytest.skip.Exception):
        info.verdict = Verdict(Status.SKIPPED, "", (), False, str(error))
        return
    if not isinstance(error, Exception):
        return
    verdict = classify(error)
    info.verdict = verdict
    info.evidence.add_error(error)
    info.step = str(getattr(error, "step", "") or "")
    detail = f"[{verdict.primary}]" + (f" step {info.step}" if info.step else "") + f" {verdict.message}"
    if verdict.secondary:
        detail += f" (secondary: {', '.join(verdict.secondary)})"
    if verdict.status is Status.SKIPPED:
        report.outcome = "skipped"
        report.longrepr = (str(item.path), (item.location[1] or 0) + 1, f"Skipped: {detail}")
    else:
        report.sections.append(("EMI outcome", detail))


def _apply_time_limit(report: pytest.TestReport, info: ItemInfo, state: HarnessState, duration: float) -> None:
    """Fail a passing test that exceeded its own time limit."""
    limit = float(info.timeout) if info.timeout else (state.resolved["test_timeout"] if state.resolved else 90.0)
    if duration > limit:
        message = f"test took {duration:.1f} s; the limit is {limit:.1f} s"
        info.verdict = Verdict(Status.FAILED, CaseKeyword.TIMEOUT, (), False, message)
        report.outcome = "failed"
        report.longrepr = f"[{CaseKeyword.TIMEOUT}] {message}"


def _finish(item: pytest.Item, info: ItemInfo, state: HarnessState) -> TestOutcome:
    """Build, store and account for the test's outcome."""
    state.current = None
    verdict = info.verdict
    status = verdict.status if verdict else Status.PASSED
    secondary = tuple(dict.fromkeys((*(verdict.secondary if verdict else ()), *info.cleanup_keywords)))
    outcome = TestOutcome(nodeid=item.nodeid, case_id=info.case_id, priority=info.priority, status=status,
                          primary=verdict.primary if verdict else "", secondary=secondary, step=info.step,
                          message=verdict.message if verdict else "",
                          duration=round(time.monotonic() - info.started, 3) if info.started else 0.0,
                          infrastructure=verdict.infrastructure if verdict else False, job=str(state.job),
                          steps=info.steps.records if info.steps else (),
                          attachments=info.evidence.write(state.directory, info.case_id or item.nodeid))
    state.writer.write(outcome)
    state.controller.record(outcome)
    if state.tracker is not None:
        state.tracker.record(outcome)
    return outcome


def _properties(outcome: TestOutcome) -> list[tuple[str, object]]:
    """JUnit properties: keywords, step and the step log."""
    steps = "; ".join(f"{record.label} {record.status} {record.duration:.2f}s"
                      + (f" {record.primary}" if record.primary else "") for record in outcome.steps)
    return [("emi_case_id", outcome.case_id), ("emi_priority", outcome.priority), ("emi_status", str(outcome.status)),
            ("emi_primary", outcome.primary), ("emi_secondary", ",".join(outcome.secondary)),
            ("emi_step", outcome.step), ("emi_steps", steps)]


# --- summary ---------------------------------------------------------------------------------------------

def pytest_terminal_summary(terminalreporter: Any, config: pytest.Config) -> None:
    """Priority decisions, unmatched names, the stop decision, a keyword summary and calibration.

    Args:
        terminalreporter: pytest's terminal reporter.
        config: pytest's config.
    """
    state = config.stash.get(_STATE, None)
    if state is None or not state.directory.is_dir():
        return
    results = {outcome.nodeid: outcome for outcome in load_results(state.directory)}
    if not results:
        return
    terminalreporter.section(f"EMI job {state.job} ({state.directory})")
    collection = _read_json(state.directory / _COLLECTION_FILE)
    if collection.get("priorities"):
        terminalreporter.write_line(str(collection["priorities"]))
    for name in collection.get("unmatched", []):
        terminalreporter.write_line(f"--test {name!r} matched no test")
    decision: StopDecision | None = state.controller.stopped()
    if decision is not None:
        terminalreporter.write_line(f"JOB STOPPED: {decision.primary} {', '.join(decision.secondary)} - "
                                    f"{decision.message}")
    counts: dict[str, int] = {}
    for outcome in results.values():
        if not outcome.passed:
            key = f"{outcome.status} {outcome.primary or '-'}"
            counts[key] = counts.get(key, 0) + 1
    for key, count in sorted(counts.items(), key=lambda pair: (-pair[1], pair[0])):
        terminalreporter.write_line(f"{count:5d}  {key}")
    if config.option.emi_calibrate:
        calibration = calibrate(results.values())
        (state.directory / "calibration.json").write_text(json.dumps(calibration, indent=2), encoding="utf-8")
        terminalreporter.write_line(f"calibration: {json.dumps(calibration['tests'])} "
                                    f"(per step: {state.directory / 'calibration.json'})")


def _read_json(path: Path) -> dict[str, Any]:
    """A JSON object file, or {}."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}

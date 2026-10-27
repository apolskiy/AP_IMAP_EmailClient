# SPDX-FileCopyrightText: 2026 Aleksandr Polskiy
# SPDX-License-Identifier: Apache-2.0
"""Numbered, verifiable steps (design §9.2).

```python
with steps.precondition("Greenmail and test broker are running") as step:   # PRE
    step.verify("health OK", api.health())
with steps.step("Send 1 activation email", timeout=10) as step:             # S01
    manifest = api.sender.send("activation", address=run.address(), count=1)
    step.verify("manifest lists 1 email", len(manifest) == 1)
```

* ``PRE`` steps come first, then the optional Step 0 (``steps.setup``), then ``S01``, ``S02``, ...; any other
  order is a condition error (SKIP ``CASE_CONDITION_INVALID_INPUT``).
* An exception inside a step is caught, keyword-tagged and recorded against the step. A failed ``PRE`` step
  is a SKIP (environment); a failed action or verification FAILS the test at that step, and the test stops.
* Time limits are checked when a step ends: blocking IMAP/SMTP calls carry their own timeouts and cannot be
  interrupted safely, so an over-long step fails with ``CASE_STEP_TIMEOUT`` once it returns. ``step.remaining``
  gives the time left, to pass on as an operation's timeout.
* :meth:`Steps.attach` adds data to the test's reports (:mod:`emi_harness.evidence`).
* Cleanups registered with :meth:`Steps.cleanup` always run (latest first); their errors are recorded as the
  secondary keyword ``CASE_CLEANUP_FAILED`` and never hide the original outcome.
"""

import logging
import time
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager, nullcontext
from typing import Any

from emi_harness.evidence import Evidence
from emi_harness.keywords import CaseKeyword
from emi_harness.outcomes import CaseFailure, CaseSkip, Status, StepRecord, classify

_LOG = logging.getLogger("emi_harness.steps")
StepHook = Callable[[str], AbstractContextManager[Any]]


class Step:
    """One running step; use :meth:`verify` for each check."""

    def __init__(self, label: str, description: str, timeout: float, clock: Callable[[], float]) -> None:
        """Start the step.

        Args:
            label: ``PRE``, ``S00``, ``S01``...
            description: What the step does.
            timeout: Seconds allowed.
            clock: Monotonic clock.
        """
        self._label = label
        self._description = description
        self._timeout = timeout
        self._clock = clock
        self._started = clock()
        self._verifications: list[tuple[str, bool]] = []

    @property
    def label(self) -> str:
        """The step label."""
        return self._label

    @property
    def description(self) -> str:
        """What the step does."""
        return self._description

    @property
    def timeout(self) -> float:
        """Seconds allowed."""
        return self._timeout

    @property
    def elapsed(self) -> float:
        """Seconds since the step started."""
        return self._clock() - self._started

    @property
    def remaining(self) -> float:
        """Seconds left before the step's limit (never negative)."""
        return max(0.0, self._timeout - self.elapsed)

    @property
    def verifications(self) -> tuple[tuple[str, bool], ...]:
        """Checks made so far."""
        return tuple(self._verifications)

    def verify(self, description: str, condition: Any) -> None:
        """Check one condition; a false one ends the step.

        Args:
            description: What is checked.
            condition: A truth value, or an object with ``ready`` and ``keywords`` (``api.health()``), whose
                keywords then explain a failure.

        Raises:
            CaseFailure: ``CASE_VERIFICATION_FAILED`` (or the health keywords) when the condition is false.
        """
        keywords: tuple[str, tuple[str, ...]] = ("", ())
        if hasattr(condition, "ready") and hasattr(condition, "keywords"):
            keywords = condition.keywords
            condition = condition.ready
        passed = bool(condition)
        self._verifications.append((description, passed))
        _LOG.info("%s VERIFY %s: %s", self._label, description, "ok" if passed else "FAILED")
        if not passed:
            primary = keywords[0] or CaseKeyword.VERIFICATION_FAILED
            raise CaseFailure(f"verification failed: {description}", primary, secondary=keywords[1],
                              step=self._label)


class Steps:  # pylint: disable=too-many-instance-attributes
    """The step log of one test."""

    def __init__(
        self,
        *,
        step_timeout: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
        hook: StepHook | None = None,
        evidence: Evidence | None = None,
    ) -> None:
        """Create an empty step log.

        Args:
            step_timeout: Default seconds per step.
            clock: Monotonic clock.
            hook: Wraps each step for a report (e.g. ``allure.step``).
            evidence: Where :meth:`attach` puts data for the reports.
        """
        self._step_timeout = step_timeout
        self._clock = clock
        self._hook = hook
        self._evidence = evidence or Evidence()
        self._records: list[StepRecord] = []
        self._cleanups: list[tuple[str, Callable[[], None]]] = []
        self._numbered = 0
        self._setup_done = False
        self._ended: StepRecord | None = None

    @property
    def records(self) -> tuple[StepRecord, ...]:
        """Every finished step."""
        return tuple(self._records)

    @property
    def failed_step(self) -> StepRecord | None:
        """The step that ended the test, if any."""
        return self._ended

    def precondition(self, description: str, timeout: float | None = None) -> AbstractContextManager[Step]:
        """A ``PRE`` step: environment checks; a failure SKIPS the test.

        Args:
            description: What is checked.
            timeout: Seconds allowed (default per-step limit).

        Returns:
            The step as a context manager.
        """
        self._check_order(self._setup_done or self._numbered > 0, "PRE steps must come before Step 0 and S01")
        return self._run("PRE", description, timeout, environment=True)

    def setup(self, description: str, timeout: float | None = None) -> AbstractContextManager[Step]:
        """The optional Step 0: test-specific setup.

        Args:
            description: What is set up.
            timeout: Seconds allowed.

        Returns:
            The step as a context manager.
        """
        self._check_order(self._setup_done or self._numbered > 0, "Step 0 may appear once, before S01")
        self._setup_done = True
        return self._run("S00", description, timeout, environment=False)

    def step(self, description: str, timeout: float | None = None) -> AbstractContextManager[Step]:
        """The next numbered step (``S01``, ``S02``, ...).

        Args:
            description: The action and what it should achieve.
            timeout: Seconds allowed.

        Returns:
            The step as a context manager.
        """
        self._numbered += 1
        return self._run(f"S{self._numbered:02d}", description, timeout, environment=False)

    @property
    def evidence(self) -> Evidence:
        """The test's evidence."""
        return self._evidence

    def attach(self, name: str, data: Any) -> None:
        """Attach data to the test's reports (results files, HTML, Allure).

        Args:
            name: What it is.
            data: JSON-ready data.
        """
        self._evidence.add(name, data)

    def cleanup(self, description: str, action: Callable[[], None]) -> None:
        """Register teardown that always runs, latest first.

        Args:
            description: What is cleaned up.
            action: The teardown.
        """
        self._cleanups.append((description, action))

    def run_cleanups(self) -> tuple[str, ...]:
        """Run every cleanup (latest first), never raising.

        Returns:
            Secondary keywords to add: ``CASE_CLEANUP_FAILED`` once if any cleanup raised.
        """
        failed = False
        while self._cleanups:
            description, action = self._cleanups.pop()
            try:
                action()
            except Exception:  # pylint: disable=broad-exception-caught
                # Cleanup must never hide the original outcome; the error is logged with its traceback.
                _LOG.exception("%s cleanup '%s' failed", CaseKeyword.CLEANUP_FAILED, description)
                failed = True
        return (CaseKeyword.CLEANUP_FAILED,) if failed else ()

    # --- helpers ---------------------------------------------------------------------------------------

    def _check_order(self, wrong: bool, message: str) -> None:
        """Refuse a step out of order."""
        if wrong:
            raise CaseSkip(f"invalid step order: {message}", CaseKeyword.CONDITION_INVALID_INPUT)

    @contextmanager
    def _run(self, label: str, description: str, timeout: float | None,
             environment: bool) -> Iterator[Step]:
        """Run one step: log, time, classify its error, record it."""
        limit = self._step_timeout if timeout is None else float(timeout)
        if limit <= 0:
            raise CaseSkip(f"step {label} time limit must be positive, got {limit}",
                           CaseKeyword.CONDITION_INVALID_INPUT, step=label)
        current = Step(label, description, limit, self._clock)
        _LOG.info("%s START %s", label, description)
        wrapper = self._hook(f"{label} {description}") if self._hook else nullcontext()
        try:
            with wrapper:
                yield current
        except BaseException as error:  # pylint: disable=broad-exception-caught
            # Every error inside a step is recorded against it, then re-raised as a typed outcome.
            self._fail(current, error, environment)
        self._finish(current)

    def _finish(self, current: Step) -> None:
        """Record a step that returned; enforce its time limit."""
        duration = current.elapsed
        if duration > current.timeout:
            message = f"step took {duration:.1f} s; the limit is {current.timeout:.1f} s"
            self._record(current, Status.FAILED, CaseKeyword.STEP_TIMEOUT, message, duration)
            raise CaseFailure(message, CaseKeyword.STEP_TIMEOUT, step=current.label)
        self._record(current, Status.PASSED, "", "", duration)

    def _fail(self, current: Step, error: BaseException, environment: bool) -> None:
        """Record a step that raised and re-raise it as SKIP or FAIL."""
        if not isinstance(error, Exception):
            raise error  # KeyboardInterrupt and pytest's own outcomes pass through untouched
        verdict = classify(error)
        skipped = environment or verdict.status is Status.SKIPPED
        status = Status.SKIPPED if skipped else Status.FAILED
        self._record(current, status, verdict.primary, verdict.message, current.elapsed)
        if skipped:
            raise CaseSkip(verdict.message, verdict.primary, secondary=verdict.secondary, step=current.label,
                           infrastructure=environment or verdict.infrastructure) from error
        raise CaseFailure(verdict.message, verdict.primary, secondary=verdict.secondary,
                          step=current.label) from error

    @property
    def verification_count(self) -> int:
        """How many ``verify`` calls the finished steps made (design §13.4)."""
        return sum(len(record.verifications) for record in self._records)

    def _record(self, current: Step, status: Status, primary: str, message: str, duration: float) -> None:
        """Store a step's record and log its end."""
        record = StepRecord(label=current.label, description=current.description, status=status,
                            duration=round(duration, 3), verifications=current.verifications, primary=primary,
                            message=message, limit=current.timeout)
        self._records.append(record)
        if status is not Status.PASSED:
            self._ended = record
        _LOG.info("%s END %s (%.3f s)%s", current.label, status, duration, f" {primary}" if primary else "")

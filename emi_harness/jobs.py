# SPDX-FileCopyrightText: 2026 Aleksandr Polskiy
# SPDX-License-Identifier: Apache-2.0
"""When a job stops (design §9.4).

* An infrastructure failure (failed PRE step, or a network fault retries did not cure) stops any job.
* Every P0 test is critical: a P0 failure is ``JOB_CRITICAL_FUNCTIONAL_FAILURE`` and aborts the job.
* Ordinary functional failures are isolated.
* Circuit breaker (P2-P4 and debug runs): 5 failures in a row with the same primary keyword ->
  ``JOB_CIRCUIT_BREAKER_OPEN``; a pass or a different keyword resets the count; skips do neither.
* Job time budget exceeded -> ``JOB_TIMEOUT``.

Once stopped, every remaining test is a SKIP carrying the job keyword and the keyword that caused it. With
pytest-xdist the decision is shared through a stop file in the job's report directory.
"""

import json
from dataclasses import dataclass
from pathlib import Path

from emi_harness.keywords import JobKeyword
from emi_harness.outcomes import Status, TestOutcome
from emi_harness.priorities import Job

BREAKER_LIMIT = 5
STOP_FILE = "stop.json"


@dataclass(frozen=True)
class StopDecision:
    """Why the job stopped.

    Attributes:
        primary: The job keyword, or the infrastructure keyword itself.
        secondary: The keyword that triggered it.
        message: Human-readable reason naming the test.
    """

    primary: str
    secondary: tuple[str, ...]
    message: str

    def to_json(self) -> dict[str, object]:
        """As plain JSON.

        Returns:
            A dictionary.
        """
        return {"primary": self.primary, "secondary": list(self.secondary), "message": self.message}


class JobController:
    """Watches results as they arrive and decides when the job stops."""

    def __init__(self, job: Job, *, budget_sec: float | None = None, stop_dir: Path | None = None) -> None:
        """Create the controller.

        Args:
            job: The running job.
            budget_sec: Job time budget; None for no limit.
            stop_dir: Directory of the shared stop file (pytest-xdist); None keeps the decision local.
        """
        self._job = job
        self._budget = budget_sec
        self._stop_dir = stop_dir
        self._decision: StopDecision | None = None
        self._streak_keyword = ""
        self._streak = 0

    @property
    def job(self) -> Job:
        """The running job."""
        return self._job

    def stopped(self, elapsed: float = 0.0) -> StopDecision | None:
        """The stop decision, if any (own, another worker's, or the time budget).

        Args:
            elapsed: Seconds since the job started.

        Returns:
            The decision, or None while the job continues.
        """
        if self._decision is None:
            self._decision = self._read_shared()
        if self._decision is None and self._budget is not None and elapsed > self._budget:
            self._stop(StopDecision(JobKeyword.TIMEOUT, (),
                                    f"job time budget of {self._budget:.0f} s exceeded after {elapsed:.0f} s"))
        return self._decision

    def record(self, outcome: TestOutcome) -> StopDecision | None:
        """Account for one finished test.

        Args:
            outcome: The test's result.

        Returns:
            The stop decision when this result stops the job, else None.
        """
        if self._decision is not None:
            return None
        decision = self._decide(outcome)
        if decision is not None:
            self._stop(decision)
        return decision

    def _decide(self, outcome: TestOutcome) -> StopDecision | None:
        """Apply the stop rules to one result."""
        name = outcome.case_id or outcome.nodeid
        if outcome.status is Status.SKIPPED:
            if outcome.infrastructure:
                return StopDecision(outcome.primary, tuple(outcome.secondary),
                                    f"infrastructure failure in {name} step {outcome.step or '-'}: {outcome.message}")
            return None
        if outcome.status is Status.PASSED:
            self._streak_keyword, self._streak = "", 0
            return None
        if outcome.priority == "P0":
            return StopDecision(JobKeyword.CRITICAL_FUNCTIONAL_FAILURE, (outcome.primary,),
                                f"P0 test {name} failed with {outcome.primary}")
        if not self._job.breaker:
            return None
        if outcome.primary == self._streak_keyword:
            self._streak += 1
        else:
            self._streak_keyword, self._streak = outcome.primary, 1
        if self._streak >= BREAKER_LIMIT:
            return StopDecision(JobKeyword.CIRCUIT_BREAKER_OPEN, (outcome.primary,),
                                f"{self._streak} failures in a row with {outcome.primary}, last {name}")
        return None

    def _stop(self, decision: StopDecision) -> None:
        """Keep the decision and share it with the other workers (the first one written wins)."""
        self._decision = decision
        if self._stop_dir is None:
            return
        self._stop_dir.mkdir(parents=True, exist_ok=True)
        try:
            with (self._stop_dir / STOP_FILE).open("x", encoding="utf-8") as handle:
                json.dump(decision.to_json(), handle)
        except FileExistsError:
            self._decision = self._read_shared() or decision

    def _read_shared(self) -> StopDecision | None:
        """Another worker's decision, if one was written."""
        return read_stop(self._stop_dir) if self._stop_dir is not None else None


def read_stop(directory: Path) -> StopDecision | None:
    """Read a job's stop file.

    Args:
        directory: The job's report directory.

    Returns:
        The decision, or None when the job did not stop (or the file is unreadable).
    """
    path = directory / STOP_FILE
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return StopDecision(str(data.get("primary", "")), tuple(data.get("secondary", ())), str(data.get("message", "")))

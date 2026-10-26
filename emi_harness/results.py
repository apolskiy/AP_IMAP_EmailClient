# SPDX-FileCopyrightText: 2026 Aleksandr Polskiy
# SPDX-License-Identifier: Apache-2.0
"""Where a run's results live (design §9.8).

``reports/<run>/<job>/``: ``junit.xml``, ``report.html``, ``results-<worker>.jsonl`` (one JSON line per test,
read by the gates, the dependency tracker and ``--keyword`` selection), ``stop.json`` when the job stopped,
and ``calibration.json`` after ``--calibrate``. ``<run>`` is the run ID without its worker part, so every
job and worker of one run shares it.
"""

import json
import math
import re
import threading
from collections import defaultdict
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from emi_harness.outcomes import TestOutcome

_WORKER = re.compile(r"-w\d{2,}-")


def run_folder(run_id: str) -> str:
    """The run's folder name: the run ID without the worker part.

    Args:
        run_id: e.g. ``20261026t153012-w03-a1b2``.

    Returns:
        e.g. ``20261026t153012-a1b2``.
    """
    return _WORKER.sub("-", run_id, count=1)


def job_dir(reports_dir: Path, run_id: str, job: str) -> Path:
    """One job's report directory.

    Args:
        reports_dir: Root of all reports.
        run_id: Any worker's run ID.
        job: Job name (``P0``, ``P1``, ``P2-P4``, ``debug``, ``all``).

    Returns:
        ``<reports>/<run>/<job>``.
    """
    return reports_dir / run_folder(run_id) / job


class ResultsWriter:
    """Appends one JSON line per finished test to the worker's results file."""

    def __init__(self, directory: Path, worker: str) -> None:
        """Prepare the file (created on the first write, so runs without functional tests leave nothing).

        Args:
            directory: The job's report directory.
            worker: Worker name (``gw0``, or ``main`` without workers).
        """
        self._path = directory / f"results-{worker}.jsonl"
        self._lock = threading.Lock()

    @property
    def path(self) -> Path:
        """The results file."""
        return self._path

    def write(self, outcome: TestOutcome) -> None:
        """Append one result.

        Args:
            outcome: The test's result.
        """
        line = json.dumps(outcome.to_json(), ensure_ascii=False)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock, self._path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")


def load_results(directory: Path) -> list[TestOutcome]:
    """Every result under a directory (a job, a run, or all reports), oldest file first.

    Args:
        directory: Where to look, recursively.

    Returns:
        The results; unreadable lines are skipped.
    """
    outcomes: list[TestOutcome] = []
    if not directory.is_dir():
        return outcomes
    for path in sorted(directory.rglob("results-*.jsonl"), key=lambda item: item.stat().st_mtime):
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                outcomes.append(TestOutcome.from_json(json.loads(line)))
            except (ValueError, KeyError, TypeError):
                continue
    return outcomes


def latest_run(reports_dir: Path) -> Path | None:
    """The most recently changed run folder.

    Args:
        reports_dir: Root of all reports.

    Returns:
        The folder, or None when there is none.
    """
    if not reports_dir.is_dir():
        return None
    runs = [path for path in reports_dir.iterdir() if path.is_dir()]
    return max(runs, key=lambda path: path.stat().st_mtime) if runs else None


def _percentile(values: list[float], share: float) -> float:
    """Nearest-rank percentile of sorted values."""
    index = max(0, math.ceil(share * len(values)) - 1)
    return values[index]


def calibrate(outcomes: Iterable[TestOutcome]) -> dict[str, Any]:
    """Durations per test and per step description, with suggested limits (design §10, ``--calibrate``).

    Args:
        outcomes: Results of passing tests are measured; others would distort the numbers.

    Returns:
        ``{"tests": {...}, "steps": {description: {...}}}`` with count, p50, p95, p99 and the suggested limit
        (p99 × 2, rounded up to whole seconds, at least 1 s).
    """
    tests: list[float] = []
    steps: dict[str, list[float]] = defaultdict(list)
    for outcome in outcomes:
        if not outcome.passed:
            continue
        tests.append(outcome.duration)
        for record in outcome.steps:
            steps[record.description].append(record.duration)

    def _stats(values: list[float]) -> dict[str, float]:
        ordered = sorted(values)
        p99 = _percentile(ordered, 0.99)
        return {"count": len(ordered), "p50": _percentile(ordered, 0.50), "p95": _percentile(ordered, 0.95),
                "p99": p99, "suggested_limit": float(max(1, math.ceil(p99 * 2)))}

    return {"tests": _stats(tests) if tests else {"count": 0},
            "steps": {name: _stats(values) for name, values in sorted(steps.items())}}

# SPDX-FileCopyrightText: 2026 Aleksandr Polskiy
# SPDX-License-Identifier: Apache-2.0
"""Test doubles shared by every tool's unit tests."""


class SimClock:
    """Monotonic clock that only moves when the code under test sleeps (or a test advances it)."""

    def __init__(self) -> None:
        """Start at time zero."""
        self._now = 0.0
        self._sleeps: list[float] = []

    @property
    def current(self) -> float:
        """The simulated time."""
        return self._now

    @property
    def sleeps(self) -> list[float]:
        """Every sleep requested, in order."""
        return list(self._sleeps)

    def now(self) -> float:
        """Return the current simulated time."""
        return self._now

    def sleep(self, seconds: float) -> None:
        """Advance the simulated time by a sleep of the code under test."""
        self._sleeps.append(seconds)
        self._now += seconds

    def advance(self, seconds: float) -> None:
        """Let time pass without a sleep (work that took long)."""
        self._now += seconds

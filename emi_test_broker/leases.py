# SPDX-FileCopyrightText: 2026 Aleksandr Polskiy
# SPDX-License-Identifier: Apache-2.0
"""Run leases, active waits and the per-host connection budget (design §7, §11)."""

import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass


@dataclass
class Lease:
    """One registered run.

    Attributes:
        run_id: The run's ID.
        holder: Who holds it (host, pid, mode), for the purge refusal message.
        renewed_at: Monotonic time of the last heartbeat.
    """

    run_id: str
    holder: str
    renewed_at: float


class LeaseTable:
    """Thread-safe table of run leases and of waits in progress."""

    def __init__(self, ttl_sec: float, clock: Callable[[], float]) -> None:
        """Create an empty table.

        Args:
            ttl_sec: A lease expires after this long without a heartbeat.
            clock: Monotonic clock.
        """
        self._ttl = ttl_sec
        self._clock = clock
        self._lock = threading.Lock()
        self._leases: dict[str, Lease] = {}
        self._waits: dict[str, int] = {}

    def acquire(self, run_id: str, holder: str) -> Lease:
        """Register (or renew) a run.

        Args:
            run_id: The run's ID.
            holder: Who holds it.

        Returns:
            The lease.
        """
        with self._lock:
            lease = Lease(run_id, holder, self._clock())
            self._leases[run_id] = lease
            return lease

    def heartbeat(self, run_id: str) -> bool:
        """Renew a lease.

        Args:
            run_id: The run's ID.

        Returns:
            False when the lease had expired or never existed (the caller re-acquires).
        """
        with self._lock:
            self._expire()
            lease = self._leases.get(run_id)
            if lease is None:
                return False
            lease.renewed_at = self._clock()
            return True

    def release(self, run_id: str) -> None:
        """End a run's lease; unknown IDs are ignored.

        Args:
            run_id: The run's ID.
        """
        with self._lock:
            self._leases.pop(run_id, None)

    def active(self) -> dict[str, str]:
        """Every live lease and every run with a wait in progress.

        Returns:
            Run ID to holder (``"wait in progress"`` for waits without a lease).
        """
        with self._lock:
            self._expire()
            result = {run_id: lease.holder for run_id, lease in self._leases.items()}
            for run_id, count in self._waits.items():
                if count > 0:
                    result.setdefault(run_id, "wait in progress")
            return result

    @contextmanager
    def waiting(self, run_id: str) -> Iterator[None]:
        """Mark a wait as in progress for its duration.

        Args:
            run_id: The waiting run (may be empty for unmarked waits).

        Yields:
            Nothing.
        """
        key = run_id or "(unmarked)"
        with self._lock:
            self._waits[key] = self._waits.get(key, 0) + 1
        try:
            yield
        finally:
            with self._lock:
                self._waits[key] -= 1
                if not self._waits[key]:
                    del self._waits[key]

    def _expire(self) -> None:
        """Drop leases past their TTL (caller holds the lock)."""
        limit = self._clock() - self._ttl
        for run_id in [run_id for run_id, lease in self._leases.items() if lease.renewed_at < limit]:
            del self._leases[run_id]


class ConnectionBudget:
    """Counts IMAP/SMTP connections across every run on the host; callers queue for free slots."""

    def __init__(self, limit: int) -> None:
        """Create the budget.

        Args:
            limit: Connections allowed at once.
        """
        self._limit = limit
        self._used = 0
        self._condition = threading.Condition()

    @property
    def limit(self) -> int:
        """Connections allowed at once."""
        return self._limit

    @property
    def used(self) -> int:
        """Connections in use now."""
        with self._condition:
            return self._used

    @contextmanager
    def slots(self, count: int, timeout: float | None = None) -> Iterator[int]:
        """Hold ``count`` slots (capped at the limit), waiting until they are free.

        Args:
            count: Slots wanted.
            timeout: Seconds to queue at most; None waits without limit.

        Yields:
            The number of slots granted.

        Raises:
            TimeoutError: If the slots did not become free in time.
        """
        granted = max(1, min(count, self._limit))
        with self._condition:
            if not self._condition.wait_for(lambda: self._used + granted <= self._limit, timeout=timeout):
                raise TimeoutError(f"{granted} of {self._limit} connections not free within {timeout} s")
            self._used += granted
        try:
            yield granted
        finally:
            with self._condition:
                self._used -= granted
                self._condition.notify_all()

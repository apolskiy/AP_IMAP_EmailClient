# SPDX-FileCopyrightText: 2026 Aleksandr Polskiy
# SPDX-License-Identifier: Apache-2.0
"""Mailbox inventory records for analysis across runs (design §5.8, AMB-76).

Each wait's inventory is appended as one JSON line to ``<job dir>/inventory-<worker>.jsonl``: one file per
pytest-xdist worker, so workers never write to the same file. Never used for pass/fail; a record that
cannot be written is dropped rather than failing the test.
"""

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

INVENTORY_PREFIX = "inventory-"


class InventoryLog:
    """Appends one worker's inventory records."""

    def __init__(self, job_directory: Path, worker: str) -> None:
        """Name the worker's file; nothing is created until the first record.

        Args:
            job_directory: The job's report directory.
            worker: The pytest-xdist worker (``gw0``) or ``main``.
        """
        self._path = job_directory / f"{INVENTORY_PREFIX}{worker}.jsonl"
        self._lock = threading.Lock()

    @property
    def path(self) -> Path:
        """The worker's file."""
        return self._path

    def append(self, run_id: str, test_id: str, inventory: dict[str, Any]) -> bool:
        """Append one record.

        Args:
            run_id: The worker's run ID.
            test_id: The test's case ID.
            inventory: The wait's inventory (address, match fields, before, after).

        Returns:
            Whether the record was written.
        """
        record = {"run_id": run_id, "test_id": test_id,
                  "timestamp": datetime.now(timezone.utc).isoformat(timespec="milliseconds"), **inventory}
        try:
            with self._lock:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                with self._path.open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps(record, sort_keys=True) + "\n")
        except OSError:
            return False
        return True

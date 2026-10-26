# SPDX-FileCopyrightText: 2026 Aleksandr Polskiy
# SPDX-License-Identifier: Apache-2.0
"""Per-test evidence for the reports (design §9.8).

Each test collects what explains its result: the sender's manifests (through :meth:`emi_api.EmiApi.observe`),
anything the test attaches with ``steps.attach(name, data)``, and the diagnostic details of a typed tool error
(the reader's attempt history and folder accessibility matrix, the endpoint, ...). After the test the evidence
is written as JSON files under ``<job dir>/attachments/<case id>/``, listed in the test's results line, shown
in the HTML report, and attached to Allure when Allure results are written. The job's settings, each with its
source, go to ``<job dir>/settings.json`` once per job, since every test of the job shares them.
"""

import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

ATTACHMENTS_DIR = "attachments"
SETTINGS_FILE = "settings.json"
_SAFE = re.compile(r"[^A-Za-z0-9_.-]+")

Attacher = Callable[[str, str], None]


def _plain(value: Any) -> Any:
    """JSON-ready form of a value (unknown objects become their text)."""
    if isinstance(value, dict):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_plain(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def safe_name(value: str) -> str:
    """A file-name-safe form of a name or node ID.

    Args:
        value: Any text.

    Returns:
        The text with every unsafe run of characters replaced by ``_``.
    """
    return _SAFE.sub("_", value).strip("_") or "item"


class Evidence:
    """The evidence of one test, in the order it was added."""

    def __init__(self, attacher: Attacher | None = None) -> None:
        """Start empty.

        Args:
            attacher: Also called with ``(name, JSON text)`` for each item (e.g. Allure's attach).
        """
        self._items: list[tuple[str, Any]] = []
        self._attacher = attacher

    @property
    def items(self) -> tuple[tuple[str, Any], ...]:
        """Every ``(name, data)`` collected."""
        return tuple(self._items)

    def add(self, name: str, data: Any) -> None:
        """Add one item.

        Args:
            name: What it is (``manifest``, ``error_details``, ...).
            data: JSON-ready data; other objects are kept as text.
        """
        plain = _plain(data)
        self._items.append((name, plain))
        if self._attacher is not None:
            self._attacher(name, json.dumps(plain, indent=2, ensure_ascii=False))

    def add_error(self, error: BaseException) -> None:
        """Add the diagnostic details of an error and of the errors it was raised from.

        Args:
            error: The error that ended the test.
        """
        seen: set[int] = set()
        current: BaseException | None = error
        while current is not None and id(current) not in seen:
            seen.add(id(current))
            details = getattr(current, "details", None)
            if isinstance(details, dict) and details:
                self.add("error_details", {"error": type(current).__name__,
                                           "primary": str(getattr(current, "primary", "")), **details})
                return
            current = current.__cause__ or current.__context__

    def write(self, job_directory: Path, owner: str) -> tuple[str, ...]:
        """Write each item as a JSON file.

        Args:
            job_directory: The job's report directory.
            owner: The test's case ID, or its node ID.

        Returns:
            The files' paths relative to the job directory, in order.
        """
        if not self._items:
            return ()
        folder = job_directory / ATTACHMENTS_DIR / safe_name(owner)
        folder.mkdir(parents=True, exist_ok=True)
        paths: list[str] = []
        for index, (name, data) in enumerate(self._items, 1):
            path = folder / f"{index:02d}_{safe_name(name)}.json"
            path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
            paths.append(path.relative_to(job_directory).as_posix())
        return tuple(paths)

    def sections(self) -> list[tuple[str, str]]:
        """The items as report sections (pytest-html shows them under the test).

        Returns:
            ``(title, JSON text)`` pairs.
        """
        return [(f"EMI evidence: {name}", json.dumps(data, indent=2, ensure_ascii=False))
                for name, data in self._items]


def write_settings(job_directory: Path, described: dict[str, Any]) -> Path:
    """Write the job's settings, each with its source (secrets redacted by the caller).

    Args:
        job_directory: The job's report directory.
        described: ``Resolved.describe()``.

    Returns:
        The file written.
    """
    job_directory.mkdir(parents=True, exist_ok=True)
    path = job_directory / SETTINGS_FILE
    path.write_text(json.dumps(_plain(described), indent=2), encoding="utf-8")
    return path

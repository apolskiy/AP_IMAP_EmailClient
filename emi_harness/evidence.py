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
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ATTACHMENTS_DIR = "attachments"
SETTINGS_FILE = "settings.json"
SECTION_LIMIT = 20 * 1024
_SAFE = re.compile(r"[^A-Za-z0-9_.-]+")

Attacher = Callable[[str, str], None]
FileAttacher = Callable[[str, bytes, str, str], None]


@dataclass(frozen=True)
class EvidenceFile:
    """A non-JSON evidence item: a raw email (``.eml``) or log lines (``.log``).

    Attributes:
        content: The bytes written.
        suffix: File extension without the dot.
        mime: Media type for Allure.
    """

    content: bytes
    suffix: str
    mime: str


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

    def __init__(self, attacher: Attacher | None = None, file_attacher: FileAttacher | None = None) -> None:
        """Start empty.

        Args:
            attacher: Also called with ``(name, JSON text)`` for each item (e.g. Allure's attach).
            file_attacher: Also called with ``(name, bytes, media type, suffix)`` for each file item.
        """
        self._items: list[tuple[str, Any]] = []
        self._attacher = attacher
        self._file_attacher = file_attacher

    @property
    def items(self) -> tuple[tuple[str, Any], ...]:
        """Every ``(name, data)`` collected; a file item's data is an :class:`EvidenceFile`."""
        return tuple(self._items)

    def add_file(self, name: str, content: bytes, suffix: str, mime: str) -> None:
        """Add one file item (a raw email, log lines).

        Args:
            name: What it is (``email_INBOX_42``, ``broker_log``).
            content: The bytes.
            suffix: File extension without the dot (``eml``, ``log``).
            mime: Media type (``message/rfc822``, ``text/plain``).
        """
        self._items.append((name, EvidenceFile(content, suffix, mime)))
        if self._file_attacher is not None:
            self._file_attacher(name, content, mime, suffix)

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
            if isinstance(data, EvidenceFile):
                path = folder / f"{index:02d}_{safe_name(name)}.{data.suffix}"
                path.write_bytes(data.content)
            else:
                path = folder / f"{index:02d}_{safe_name(name)}.json"
                path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
            paths.append(path.relative_to(job_directory).as_posix())
        return tuple(paths)

    def sections(self) -> list[tuple[str, str]]:
        """The items as report sections (pytest-html shows them under the test).

        Returns:
            ``(title, text)`` pairs: JSON items in full, file items as text up to ``SECTION_LIMIT`` bytes.
        """
        return [(f"EMI evidence: {name}", _section_text(data)) for name, data in self._items]


def _section_text(data: Any) -> str:
    """A report section's text: JSON, or a file's first ``SECTION_LIMIT`` bytes."""
    if not isinstance(data, EvidenceFile):
        return json.dumps(data, indent=2, ensure_ascii=False)
    text = data.content[:SECTION_LIMIT].decode("utf-8", errors="replace")
    if len(data.content) > SECTION_LIMIT:
        text += f"\n... ({len(data.content) - SECTION_LIMIT} more bytes in the attachment file)"
    return text


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

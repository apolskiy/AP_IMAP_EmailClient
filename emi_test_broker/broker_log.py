# SPDX-FileCopyrightText: 2026 Aleksandr Polskiy
# SPDX-License-Identifier: Apache-2.0
"""The broker's dated log: ``logs/emi_test_broker_YYYY-MM-DD.log`` (design §7).

Entries carry ISO 8601 local time with UTC offset. Each date has one file, appended to; when it cannot be
opened for appending (locked, read-only), ``emi_test_broker_YYYY-MM-DD_<n>.log`` is created instead.
"""

import logging
from datetime import date, datetime
from pathlib import Path

from emi_test_broker.keywords import TestBrokerKeyword

LOGGER_NAME = "emi_test_broker"
_PREFIX = "emi_test_broker_"
_MAX_ALTERNATES = 100


class IsoFormatter(logging.Formatter):
    """``<ISO 8601 local time with offset> <LEVEL> <message>``."""

    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:  # noqa: N802
        """Format the record time as ISO 8601 local time with UTC offset.

        Args:
            record: The log record.
            datefmt: Ignored.

        Returns:
            For example ``2026-10-26T15:30:12.123+01:00``.
        """
        del datefmt
        return datetime.fromtimestamp(record.created).astimezone().isoformat(timespec="milliseconds")


def log_path(log_dir: Path, day: date, alternate: int = 0) -> Path:
    """Path of the log for a date.

    Args:
        log_dir: Log directory.
        day: The date.
        alternate: 0 for the main file, ``n`` for ``_<n>``.

    Returns:
        The file path.
    """
    suffix = f"_{alternate}" if alternate else ""
    return log_dir / f"{_PREFIX}{day.isoformat()}{suffix}.log"


def open_log_handler(log_dir: Path, day: date) -> logging.FileHandler:
    """Open the date's log for appending, falling back to ``_<n>`` files.

    Args:
        log_dir: Log directory (created if needed).
        day: The date.

    Returns:
        A handler with the ISO formatter.

    Raises:
        OSError: If no file of that date can be opened.
    """
    log_dir.mkdir(parents=True, exist_ok=True)
    last_error: OSError | None = None
    for alternate in range(_MAX_ALTERNATES):
        try:
            handler = logging.FileHandler(log_path(log_dir, day, alternate), mode="a", encoding="utf-8")
        except OSError as error:
            last_error = error
            continue
        handler.setFormatter(IsoFormatter("%(asctime)s %(levelname)s %(message)s"))
        return handler
    raise last_error or OSError(f"cannot open a log file in {log_dir}")


def configure(log_dir: Path, day: date | None = None) -> logging.Logger:
    """Attach the dated file handler to the broker logger (once per process and date).

    Args:
        log_dir: Log directory.
        day: The date; today by default.

    Returns:
        The broker logger.
    """
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(logging.INFO)
    # Entries belong in the dated file only; propagating would copy them into whatever the root logger
    # writes to (for a spawned broker: the start-errors file, which must stay for real start failures).
    logger.propagate = False
    wanted = log_path(log_dir, day or date.today()).resolve()
    if not any(isinstance(handler, logging.FileHandler) and Path(handler.baseFilename).resolve() == wanted
               for handler in logger.handlers):
        logger.addHandler(open_log_handler(log_dir, day or date.today()))
    return logger


def log_port_unavailable(log_dir: Path, port: int, reason: str) -> None:
    """Record a "port could not be opened" entry (kept for every debug report of that date).

    Args:
        log_dir: Log directory.
        port: The port.
        reason: What was found on it.
    """
    configure(log_dir).error("%s port %d could not be opened: %s", TestBrokerKeyword.PORT_UNAVAILABLE, port,
                             reason)


def debug_entries(log_dir: Path, start: datetime, end: datetime) -> list[str]:
    """Entries for a debug report: every entry in the run's window plus every port failure of those dates.

    Args:
        log_dir: Log directory.
        start: Window start (aware).
        end: Window end (aware).

    Returns:
        Log lines in file order.
    """
    lines: list[str] = []
    day = start.astimezone().date()
    while day <= end.astimezone().date():
        for path in sorted(log_dir.glob(f"{_PREFIX}{day.isoformat()}*.log")):
            for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
                stamp = _timestamp(line)
                in_window = stamp is not None and start <= stamp <= end
                if in_window or TestBrokerKeyword.PORT_UNAVAILABLE in line:
                    lines.append(line)
        day = date.fromordinal(day.toordinal() + 1)
    return lines


def _timestamp(line: str) -> datetime | None:
    """The leading ISO timestamp of a log line, or None."""
    head = line.split(" ", 1)[0]
    try:
        return datetime.fromisoformat(head)
    except ValueError:
        return None

# SPDX-FileCopyrightText: 2026 Aleksandr Polskiy
# SPDX-License-Identifier: Apache-2.0
"""Raw emails and broker log lines for a test that failed or was skipped (design §9.8, AMB-74).

After such a test the harness asks the reader for every email at the test's addresses (its own and those in
its manifests), matched or not, and reads the broker's log lines of the test's time window that name it.
Collecting never changes the test's result: a failure to collect is itself noted in the evidence.
"""

from collections.abc import Callable, Iterable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from emi_harness.evidence import Evidence, EvidenceFile
from emi_reader import RawEmail
from emi_test_broker.broker_log import debug_entries

EMAIL_LIMIT = 30
EMAIL_SIZE_LIMIT = 256 * 1024

EmailFetcher = Callable[[tuple[str, ...], int], list[RawEmail]]


def test_addresses(own: str, items: Iterable[tuple[str, Any]], case_id: str = "") -> tuple[str, ...]:
    """The test's own address and every address in its own sender manifests, in first-seen order.

    A manifest marked for another case is left out: the session's probe email (case ``C000000``) is sent
    while the first test sets up, so its manifest can sit in that test's evidence.

    Args:
        own: The test's own address (``EmiRun.address()``).
        items: The test's evidence items so far.
        case_id: The test's case ID; manifests marked for another case are skipped.

    Returns:
        The addresses, lower-cased, without duplicates.
    """
    found = [own] if own else []
    for name, data in items:
        if name != "manifest" or not isinstance(data, dict):
            continue
        marked = str(data.get("test_id") or "")
        if case_id and marked and marked.upper() != case_id.upper():
            continue
        found.extend(str(entry.get("address", "")) for entry in data.get("entries", []))
    return tuple(dict.fromkeys(address.strip().lower() for address in found if address.strip()))


def add_emails(evidence: Evidence, emails: list[RawEmail]) -> None:
    """Add each email as an ``.eml`` item, cut at ``EMAIL_SIZE_LIMIT``, and a JSON index of them.

    Args:
        evidence: The test's evidence.
        emails: The emails, newest first.
    """
    index = []
    for email in emails:
        truncated = len(email.raw) > EMAIL_SIZE_LIMIT
        name = f"email_{email.folder}_{email.uid}" + ("_truncated" if truncated else "")
        index.append({"folder": email.folder, "uid": email.uid, "address": email.address, "subject": email.subject,
                      "size": len(email.raw), "truncated": truncated})
        evidence.add_file(name, email.raw[:EMAIL_SIZE_LIMIT], "eml", "message/rfc822")
    evidence.add("emails", {"count": len(emails), "limit": EMAIL_LIMIT, "emails": index})


def broker_lines(log_dir: Path, start: datetime, end: datetime, *, case_id: str, run_id: str,
                 addresses: tuple[str, ...]) -> list[str]:
    """The broker log lines of the test's window that name it.

    A line names the test when it carries both ``test=<case>`` and ``run=<run>`` (sends) or one of the
    test's addresses (waits, email lookups). Other tests' lines in the same window are left out.

    Args:
        log_dir: The broker's log directory.
        start: Test start (aware).
        end: Test end (aware).
        case_id: The test's case ID.
        run_id: This worker's run ID.
        addresses: The test's addresses.

    Returns:
        The lines in file order.
    """
    lowered = tuple(address.lower() for address in addresses)

    def _names_test(line: str) -> bool:
        if case_id and f"test={case_id}" in line and f"run={run_id}" in line:
            return True
        text = line.lower()
        return any(address in text for address in lowered)

    return [line for line in debug_entries(log_dir, start, end) if _names_test(line)]


def add_broker_lines(evidence: Evidence, lines: list[str]) -> None:
    """Add the lines as a ``.log`` item (an empty window is noted in JSON instead).

    Args:
        evidence: The test's evidence.
        lines: Broker log lines.
    """
    if lines:
        evidence.add_file("broker_log", ("\n".join(lines) + "\n").encode("utf-8"), "log", "text/plain")
    else:
        evidence.add("broker_log", {"lines": 0, "note": "no broker log line names this test in its time window"})


def collect(evidence: Evidence, *, fetch: EmailFetcher, own_address: str, log_dir: Path | None,
            case_id: str, run_id: str, seconds: float) -> None:
    """Add a failed or skipped test's emails and broker log lines; never raises.

    Args:
        evidence: The test's evidence (its manifests name the other addresses).
        fetch: ``api.reader.emails_to``.
        own_address: The test's own address.
        log_dir: The broker's log directory, or None in in-process mode (no broker).
        case_id: The test's case ID.
        run_id: This worker's run ID.
        seconds: How long the test has run (its log window).
    """
    addresses = test_addresses(own_address, evidence.items, case_id)
    try:
        add_emails(evidence, fetch(addresses, EMAIL_LIMIT))
    except Exception as error:  # pylint: disable=broad-exception-caught  # evidence never changes a result
        evidence.add("emails", {"collected": False, "error": f"{type(error).__name__}: {error}",
                                "primary": str(getattr(error, "primary", ""))})
    if log_dir is None:
        evidence.add("broker_log", {"lines": 0, "note": "in-process mode has no test broker"})
        return
    end = datetime.now().astimezone()
    start = end - timedelta(seconds=seconds + 1.0)
    try:
        lines = broker_lines(log_dir, start, end, case_id=case_id, run_id=run_id, addresses=addresses)
    except OSError as error:
        evidence.add("broker_log", {"lines": 0, "note": f"broker log not readable: {error}"})
        return
    add_broker_lines(evidence, lines)


def file_names(evidence: Evidence) -> list[str]:
    """Names of the evidence's file items (for unit tests and logs).

    Args:
        evidence: The evidence.

    Returns:
        The names in order.
    """
    return [name for name, data in evidence.items if isinstance(data, EvidenceFile)]

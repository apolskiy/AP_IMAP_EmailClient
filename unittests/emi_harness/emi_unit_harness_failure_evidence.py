# SPDX-FileCopyrightText: 2026 Aleksandr Polskiy
# SPDX-License-Identifier: Apache-2.0
"""Unit tests: raw emails and broker log lines for a test that did not pass (design §9.8, AMB-74)."""

import json
from datetime import datetime, timedelta
from pathlib import Path

from emi_harness.evidence import SECTION_LIMIT, Evidence
from emi_harness.failure_evidence import (
    EMAIL_LIMIT,
    EMAIL_SIZE_LIMIT,
    broker_lines,
    collect,
    file_names,
    test_addresses,
)
from emi_reader import EmailWaitError, RawEmail, ReaderCaseKeyword
from emi_test_broker.broker_log import log_path

_OWN = "qa+C400001_20261026t153012-w00-a1b2@example.com"
_OTHER = "qa+C400001_20261026t153012-w00-a1b2-1@example.com"
_RUN = "20261026t153012-w00-a1b2"


def _email(uid: int, address: str = _OWN, size: int = 0) -> RawEmail:
    """A stored email; ``size`` pads the body."""
    raw = f"To: {address}\r\nSubject: Activate\r\n\r\nbody".encode() + b"x" * size
    return RawEmail(uid=uid, folder="INBOX", address=address, subject="Activate", raw=raw)


def _manifest(*addresses: str) -> dict[str, object]:
    """A sender manifest's JSON with one entry per address."""
    return {"entries": [{"address": address, "status": "sent"} for address in addresses]}


class TestEMIHarnessFailureEvidence:
    """What a failed or skipped test's report gets."""

    def EMI_HARNESS_UNIT_200158_file_items_written_shown_and_attached(self, tmp_path: Path) -> None:
        """File items keep the numbering, are written as-is, attached with their type and shown as text."""
        attached: list[tuple[str, str, str]] = []
        evidence = Evidence(file_attacher=lambda name, content, mime, suffix: attached.append((name, mime, suffix)))
        evidence.add("manifest", {"entries": []})
        evidence.add_file("email_INBOX_7", b"Subject: x\r\n\r\n\xff" + b"y" * SECTION_LIMIT, "eml", "message/rfc822")
        paths = evidence.write(tmp_path, "C400001")
        assert paths == ("attachments/C400001/01_manifest.json", "attachments/C400001/02_email_INBOX_7.eml")
        assert (tmp_path / paths[1]).read_bytes().startswith(b"Subject: x\r\n\r\n\xff")
        assert attached == [("email_INBOX_7", "message/rfc822", "eml")]
        title, text = evidence.sections()[1]
        assert title == "EMI evidence: email_INBOX_7" and text.endswith("more bytes in the attachment file)")

    def EMI_HARNESS_UNIT_200159_addresses_from_run_and_manifests(self) -> None:
        """The test's own address and every manifest address, once each, lower-cased."""
        probe = {**_manifest("qa+C000000_x@example.com"), "test_id": "C000000"}
        own = {**_manifest(_OWN.upper(), _OTHER), "test_id": "c400001"}
        items = [("manifest", probe), ("manifest", own), ("plan", {"address": "ignored@example.com"})]
        assert test_addresses(_OWN, items, "C400001") == (_OWN.lower(), _OTHER.lower())
        assert len(test_addresses(_OWN, items)) == 3  # without a case ID every manifest counts

    def EMI_HARNESS_UNIT_200160_emails_attached_and_cut(self, tmp_path: Path) -> None:
        """Every email becomes an .eml item, a large one cut and named so, with a JSON index."""
        evidence = Evidence()
        evidence.add("manifest", _manifest(_OTHER))
        asked: list[tuple[tuple[str, ...], int]] = []

        def _fetch(addresses: tuple[str, ...], limit: int) -> list[RawEmail]:
            asked.append((addresses, limit))
            return [_email(9, _OTHER, size=EMAIL_SIZE_LIMIT), _email(3)]

        collect(evidence, fetch=_fetch, own_address=_OWN, log_dir=None, case_id="C400001", run_id=_RUN, seconds=5.0)
        assert asked == [((_OWN.lower(), _OTHER.lower()), EMAIL_LIMIT)]
        assert file_names(evidence) == ["email_INBOX_9_truncated", "email_INBOX_3"]
        index = dict(evidence.items)["emails"]
        assert index["count"] == 2 and index["emails"][0]["truncated"] and index["emails"][0]["size"] > EMAIL_SIZE_LIMIT
        paths = evidence.write(tmp_path, "C400001")
        assert (tmp_path / paths[1]).stat().st_size == EMAIL_SIZE_LIMIT
        assert dict(evidence.items)["broker_log"]["note"] == "in-process mode has no test broker"

    def EMI_HARNESS_UNIT_200161_collecting_never_raises(self) -> None:
        """A failed lookup is noted with its keyword; the test's result is not touched."""
        evidence = Evidence()

        def _fetch(addresses: tuple[str, ...], limit: int) -> list[RawEmail]:
            raise EmailWaitError(f"{addresses} {limit}", ReaderCaseKeyword.EMAIL_NOT_RECEIVED)

        collect(evidence, fetch=_fetch, own_address=_OWN, log_dir=None, case_id="C400001", run_id=_RUN, seconds=1.0)
        emails = dict(evidence.items)["emails"]
        assert emails["collected"] is False and emails["primary"] == ReaderCaseKeyword.EMAIL_NOT_RECEIVED

    def EMI_HARNESS_UNIT_200162_broker_lines_name_the_test(self, tmp_path: Path) -> None:
        """Only lines of the window naming the test (case and run, or an address) are kept."""
        now = datetime.now().astimezone()
        lines = [f"{now.isoformat()} INFO send run={_RUN} test=C400001 emails=1 sent=1 ok",
                 f"{now.isoformat()} INFO send run={_RUN.replace('w00', 'w01')} test=C400001 emails=1 sent=1 ok",
                 f"{now.isoformat()} INFO wait run=- test=- address={_OTHER} ok",
                 f"{now.isoformat()} INFO wait run=- test=- address=qa+c400002_x@example.com ok",
                 f"{(now - timedelta(hours=1)).isoformat()} INFO wait run=- test=- address={_OWN} ok",
                 f"{now.isoformat()} ERROR TEST_BROKER_PORT_UNAVAILABLE port 8025"]
        log_path(tmp_path, now.date()).write_text("\n".join(lines), encoding="utf-8")
        found = broker_lines(tmp_path, now - timedelta(minutes=1), now + timedelta(minutes=1), case_id="C400001",
                             run_id=_RUN, addresses=(_OWN, _OTHER))
        assert found == [lines[0], lines[2]]
        evidence = Evidence()
        collect(evidence, fetch=lambda addresses, limit: [], own_address=_OWN, log_dir=tmp_path, case_id="C400001",
                run_id=_RUN, seconds=60.0)
        assert file_names(evidence) == ["broker_log"]
        empty = Evidence()
        collect(empty, fetch=lambda addresses, limit: [], own_address=_OWN, log_dir=tmp_path / "none",
                case_id="C499999", run_id=_RUN, seconds=1.0)
        assert json.dumps(dict(empty.items)["broker_log"]).count("no broker log line") == 1

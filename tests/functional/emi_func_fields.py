# SPDX-FileCopyrightText: 2026 Aleksandr Polskiy
# SPDX-License-Identifier: Apache-2.0
"""Functional tests: an address in the wrong recipient field is not matched (design §5.4, §13.3).

The positive cases (every kind found in To, Cc and Bcc) are in ``emi_func_counts``.
"""

import itertools

import pytest

from emi_api import EmiApi, EmiRun
from emi_harness import Steps
from emi_reader import EmailWaitError, ReaderCaseKeyword
from emi_sender import BatchRequest, Field
from tests.functional.catalog import CTA_OF, KINDS, MATCH_FIELDS, NEGATIVE_WAIT_SEC, cases

# (field the address is sent in, field the wait looks in)
_MISMATCHES = (("cc", "to"), ("bcc", "to"), ("to", "cc"))
CASES = [(kind, *pair) for kind, pair in itertools.product(KINDS, _MISMATCHES)]


class TestEMIFieldsNotMatched:
    """Mail is matched only in the fields the wait selects; it is still there for the right field."""

    @pytest.mark.emi(priority="P2")
    @pytest.mark.parametrize(("kind", "sent_in", "waited_in"), cases(
        402000, CASES, name=lambda value: f"{value[0]}-{value[1]}-not-{value[2]}"))
    def EMI_TOOL_FUNC_402000_wrong_field_not_matched(  # pylint: disable=too-many-arguments
        self, api: EmiApi, steps: Steps, emi_run: EmiRun, kind: str, sent_in: str, waited_in: str,
    ) -> None:
        """A wait in another field reports CASE_EMAIL_NOT_RECEIVED; a wait in the sent field finds the email."""
        with steps.step(f"Send 1 {kind} email with the address in {sent_in}", timeout=15) as step:
            manifest = api.sender.send_batch(BatchRequest(addresses=(emi_run.address(),), counts={kind: 1},
                                                          recipient_field=Field(sent_in)), marker=emi_run.marker)
            step.verify("sent", len(manifest.sent) == 1)
        with steps.step(f"Wait matched on {waited_in} only: nothing", timeout=NEGATIVE_WAIT_SEC + 10) as step:
            with pytest.raises(EmailWaitError) as caught:
                api.reader.wait_for_emails(emi_run.address(), CTA_OF[kind], timeout=NEGATIVE_WAIT_SEC,
                                           match_fields=MATCH_FIELDS[waited_in], settle_window=0.0)
            step.verify("keyword is CASE_EMAIL_NOT_RECEIVED",
                        caught.value.primary == ReaderCaseKeyword.EMAIL_NOT_RECEIVED)
        with steps.step(f"Wait matched on {sent_in}: the email is there",
                        timeout=api.settings.wait_timeout + 5) as step:
            emails = api.reader.wait_for_emails(emi_run.address(), CTA_OF[kind], timeout=step.remaining,
                                                match_fields=MATCH_FIELDS[sent_in], settle_window=0.0)
            step.verify("it is the sent email", emails[0].message_id == manifest.sent[0].message_id)

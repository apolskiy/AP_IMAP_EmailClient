# SPDX-FileCopyrightText: 2026 Aleksandr Polskiy
# SPDX-License-Identifier: Apache-2.0
"""Functional tests: an address in the wrong recipient field is not matched, and an address in several
fields is found on each of them (design §5.4, §6.3, §13.3).

The positive single-field cases (every kind found in To, Cc and Bcc) are in ``emi_func_counts``.
"""

import itertools

import pytest

from emi_api import EmiApi, EmiRun
from emi_harness import Steps
from emi_reader import EmailWaitError, ReaderCaseKeyword
from emi_sender import BatchRequest, Field, parse_fields
from tests.functional.catalog import CTA_OF, KINDS, MATCH_FIELDS, NEGATIVE_WAIT_SEC, cases

# (field the address is sent in, field the wait looks in)
_MISMATCHES = (("cc", "to"), ("bcc", "to"), ("to", "cc"))
CASES = [(kind, *pair) for kind, pair in itertools.product(KINDS, _MISMATCHES)]

# Field combinations (AMB-82, AMB-83) and a field outside each: the sender cannot produce X-Original-To,
# so it is the field outside the combination of all three.
_OUTSIDE = {"to+cc": "bcc", "to+bcc": "cc", "cc+bcc": "to", "to+cc+bcc": "x-original-to"}
COMBINATION_CASES = list(itertools.product(KINDS, _OUTSIDE))


def _match_fields(field: str) -> tuple[str, ...]:
    """The reader fields that find an address sent in ``field`` (Bcc is found by its Delivered-To)."""
    return MATCH_FIELDS.get(field, (field,))


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


class TestEMIFieldCombinations:
    """An email with the address in several recipient fields at once (AMB-82, AMB-83)."""

    @pytest.mark.emi(priority="P2")
    @pytest.mark.parametrize(("kind", "combination"), cases(
        402100, COMBINATION_CASES, name=lambda value: f"{value[0]}-{value[1]}"))
    def EMI_TOOL_FUNC_402100_combination_found_on_each_field(
        self, api: EmiApi, steps: Steps, emi_run: EmiRun, kind: str, combination: str,
    ) -> None:
        """A wait on a field outside the combination finds nothing; a wait on each of its fields finds the one
        email."""
        outside = _OUTSIDE[combination]
        with steps.step(f"Send 1 {kind} email with the address in {combination}", timeout=15) as step:
            manifest = api.sender.send_batch(BatchRequest(addresses=(emi_run.address(),), counts={kind: 1},
                                                          recipient_field=parse_fields(combination)),
                                             marker=emi_run.marker)
            step.verify("sent", len(manifest.sent) == 1)
            step.verify(f"the manifest names {combination}", manifest.sent[0].field == combination)
        with steps.step(f"Wait matched on {outside} only: nothing", timeout=NEGATIVE_WAIT_SEC + 10) as step:
            with pytest.raises(EmailWaitError) as caught:
                api.reader.wait_for_emails(emi_run.address(), CTA_OF[kind], timeout=NEGATIVE_WAIT_SEC,
                                           match_fields=_match_fields(outside), settle_window=0.0)
            step.verify("keyword is CASE_EMAIL_NOT_RECEIVED",
                        caught.value.primary == ReaderCaseKeyword.EMAIL_NOT_RECEIVED)
        for field in combination.split("+"):
            with steps.step(f"Wait matched on {field}: the email is there",
                            timeout=api.settings.wait_timeout + 5) as step:
                emails = api.reader.wait_for_emails(emi_run.address(), CTA_OF[kind], timeout=step.remaining,
                                                    match_fields=_match_fields(field), settle_window=0.0)
                step.verify("it is the sent email", emails[0].message_id == manifest.sent[0].message_id)

    @pytest.mark.emi(priority="P3")
    @pytest.mark.parametrize("kind", cases(402200, [(kind,) for kind in KINDS], name=lambda value: value[0]))
    def EMI_TOOL_FUNC_402200_delivered_to_alone(self, api: EmiApi, steps: Steps, emi_run: EmiRun, kind: str) -> None:
        """A Bcc email names the address in no visible header: a wait on To and Cc finds nothing, a wait on
        Delivered-To alone finds it."""
        with steps.step(f"Send 1 {kind} email with the address in bcc", timeout=15) as step:
            manifest = api.sender.send_batch(BatchRequest(addresses=(emi_run.address(),), counts={kind: 1},
                                                          recipient_field=Field.BCC), marker=emi_run.marker)
            step.verify("sent", len(manifest.sent) == 1)
        with steps.step("Wait matched on to and cc: nothing", timeout=NEGATIVE_WAIT_SEC + 10) as step:
            with pytest.raises(EmailWaitError) as caught:
                api.reader.wait_for_emails(emi_run.address(), CTA_OF[kind], timeout=NEGATIVE_WAIT_SEC,
                                           match_fields=("to", "cc"), settle_window=0.0)
            step.verify("keyword is CASE_EMAIL_NOT_RECEIVED",
                        caught.value.primary == ReaderCaseKeyword.EMAIL_NOT_RECEIVED)
        with steps.step("Wait matched on delivered-to only: the email is there",
                        timeout=api.settings.wait_timeout + 5) as step:
            emails = api.reader.wait_for_emails(emi_run.address(), CTA_OF[kind], timeout=step.remaining,
                                                match_fields=("delivered-to",), settle_window=0.0)
            step.verify("it is the sent email", emails[0].message_id == manifest.sent[0].message_id)

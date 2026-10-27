# SPDX-FileCopyrightText: 2026 Aleksandr Polskiy
# SPDX-License-Identifier: Apache-2.0
"""Functional tests: every count of every kind in every recipient field, and counts exceeded (design §13.3)."""

import itertools

import pytest

from emi_api import EmiApi, EmiRun
from emi_harness import Steps
from emi_reader import EmailWaitError, ReaderCaseKeyword
from emi_sender import BatchRequest, Field
from tests.functional.catalog import CTA_OF, FIELDS, KINDS, MATCH_FIELDS, cases

COUNTS = list(itertools.product(KINDS, FIELDS, range(1, 11)))
EXCEEDED = list(itertools.product(KINDS, range(1, 4)))


class TestEMICounts:
    """N emails of one kind to the test's address in one field: exactly those N arrive."""

    @pytest.mark.emi(priority="P2")
    @pytest.mark.parametrize(("kind", "field", "count"), cases(
        401000, COUNTS, name=lambda value: f"{value[0]}-{value[1]}-{value[2]}",
        priority=lambda value: "P1" if value[2] == 1 else None))
    def EMI_TOOL_FUNC_401000_counts_per_kind_and_field(  # pylint: disable=too-many-arguments
        self, api: EmiApi, steps: Steps, emi_run: EmiRun, kind: str, field: str, count: int,
    ) -> None:
        """The emails received are exactly the ones sent (by Message-ID), each with the right link."""
        with steps.step(f"Send {count} {kind} email(s) with the address in {field}", timeout=20) as step:
            manifest = api.sender.send_batch(BatchRequest(addresses=(emi_run.address(),), counts={kind: count},
                                                          recipient_field=Field(field)), marker=emi_run.marker)
            step.verify(f"manifest lists {count} sent", len(manifest.sent) == count)
        with steps.step(f"Wait for {count} email(s) matched on {field}",
                        timeout=api.settings.wait_timeout + 5) as step:
            emails = api.reader.wait_for_emails(emi_run.address(), CTA_OF[kind], count=count,
                                                timeout=step.remaining, match_fields=MATCH_FIELDS[field],
                                                settle_window=0.0)
            # Lists, not sets: an extra or duplicated email must not collapse into a match.
            step.verify(f"exactly {count} received", len(emails) == count)
            sent_ids = sorted(entry.message_id for entry in manifest.sent)
            step.verify("the received emails are the sent ones", sorted(item.message_id for item in emails) == sent_ids)
            sent_links = sorted(str(entry.link) for entry in manifest.sent)
            step.verify("each link is the one sent with it", sorted(str(item.link) for item in emails) == sent_links)


class TestEMICountExceeded:
    """One email more than expected is reported, never silently accepted."""

    @pytest.mark.emi(priority="P3")
    @pytest.mark.parametrize(("kind", "expected"), cases(
        401500, EXCEEDED, name=lambda value: f"{value[0]}-expect-{value[1]}"))
    def EMI_TOOL_FUNC_401500_count_exceeded(  # pylint: disable=too-many-arguments
        self, api: EmiApi, steps: Steps, emi_run: EmiRun, kind: str, expected: int,
    ) -> None:
        """expected + 1 emails arrive: the wait fails with CASE_EMAIL_COUNT_EXCEEDED naming both numbers."""
        with steps.step(f"Send {expected + 1} {kind} emails", timeout=20) as step:
            manifest = api.sender.send(kind, address=emi_run.address(), count=expected + 1)
            step.verify("all sent", len(manifest.sent) == expected + 1)
        with steps.step(f"Wait for {expected}: expect count exceeded", timeout=api.settings.wait_timeout + 5) as step:
            with pytest.raises(EmailWaitError) as caught:
                api.reader.wait_for_emails(emi_run.address(), CTA_OF[kind], count=expected, timeout=step.remaining,
                                           settle_window=0.0)
            step.verify("keyword is CASE_EMAIL_COUNT_EXCEEDED",
                        caught.value.primary == ReaderCaseKeyword.EMAIL_COUNT_EXCEEDED)
            step.verify("details name expected and found",
                        (caught.value.details["expected"], caught.value.details["found"]) == (expected, expected + 1))

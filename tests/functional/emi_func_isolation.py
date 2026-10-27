# SPDX-FileCopyrightText: 2026 Aleksandr Polskiy
# SPDX-License-Identifier: Apache-2.0
"""Functional tests: mail of other tests and other runs is never picked up (design §4, §13.3)."""

import itertools

import pytest

from emi_api import EmiApi, EmiRun, build_address
from emi_harness import Steps
from emi_reader import EmailWaitError, ReaderCaseKeyword
from tests.functional.catalog import CTA_OF, KINDS, NEGATIVE_WAIT_SEC, cases

_NEIGHBOURS = ("own-extra-slot", "same-case-other-run", "other-case-same-run")


def _neighbour(scenario: str, run: EmiRun, run_id: str) -> str:
    """An address that shares the shared mailbox but is not the test's own."""
    if scenario == "own-extra-slot":
        return run.address(1)
    if scenario == "same-case-other-run":
        return build_address(run.test_id, run_id + "-x")
    # Another case in the unused 59xxxx block, derived from this one so no two tests share it (C406004 -> C596004).
    return build_address(f"C59{run.test_id[3:]}", run_id)


class TestEMIIsolation:
    """Mail to a neighbouring address never satisfies the test's own wait."""

    @pytest.mark.emi(priority="P3")
    @pytest.mark.parametrize(("kind", "scenario"), cases(
        406000, list(itertools.product(KINDS, _NEIGHBOURS)), name=lambda value: f"{value[0]}-{value[1]}"))
    def EMI_TOOL_FUNC_406000_neighbour_mail_ignored(  # pylint: disable=too-many-arguments
        self, api: EmiApi, steps: Steps, emi_run: EmiRun, kind: str, scenario: str,
    ) -> None:
        """The neighbour's email is delivered (its own wait finds it) but the test's wait does not."""
        neighbour = _neighbour(scenario, emi_run, api.run_id)
        with steps.step(f"Send 1 {kind} email to the neighbour ({scenario})", timeout=15) as step:
            manifest = api.sender.send(kind, address=neighbour, count=1)
            step.verify("sent", len(manifest.sent) == 1)
        with steps.step("The neighbour's wait finds it", timeout=api.settings.wait_timeout + 5) as step:
            emails = api.reader.wait_for_emails(neighbour, CTA_OF[kind], timeout=step.remaining, settle_window=0.0)
            step.verify("it is the sent email", emails[0].message_id == manifest.sent[0].message_id)
        with steps.step("The test's own wait finds nothing", timeout=NEGATIVE_WAIT_SEC + 10) as step:
            with pytest.raises(EmailWaitError) as caught:
                api.reader.wait_for_emails(emi_run.address(), CTA_OF[kind], timeout=NEGATIVE_WAIT_SEC,
                                           settle_window=0.0)
            step.verify("keyword is CASE_EMAIL_NOT_RECEIVED",
                        caught.value.primary == ReaderCaseKeyword.EMAIL_NOT_RECEIVED)

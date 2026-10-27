# SPDX-FileCopyrightText: 2026 Aleksandr Polskiy
# SPDX-License-Identifier: Apache-2.0
"""Functional tests: test and run markers, and TestRail case IDs in either form (design §4, AMB-63)."""

import itertools

import pytest

from emi_api import EmiApi, EmiRun
from emi_harness import Steps
from emi_reader import EmailWaitError, ReaderCaseKeyword
from tests.functional.catalog import CTA_OF, KINDS, NEGATIVE_WAIT_SEC, cases

_FORMS = ("case-id", "number", "run", "case-id-and-run")


def _markers(form: str, run: EmiRun, run_id: str) -> dict[str, str]:
    """The marker arguments of one form."""
    number = run.test_id[1:]
    return {"case-id": {"test_id": run.test_id}, "number": {"test_id": number}, "run": {"run_id": run_id},
            "case-id-and-run": {"test_id": run.test_id, "run_id": run_id}}[form]


class TestEMIMarkers:
    """Waits that also require the markers find the test's own mail."""

    @pytest.mark.emi(priority="P2")
    @pytest.mark.parametrize(("kind", "form"), cases(
        403000, list(itertools.product(KINDS, _FORMS)), name=lambda value: f"{value[0]}-{value[1]}"))
    def EMI_TOOL_FUNC_403000_markers_match(  # pylint: disable=too-many-arguments
        self, api: EmiApi, steps: Steps, emi_run: EmiRun, kind: str, form: str,
    ) -> None:
        """The marker headers carry the TestRail case ID; C403000 and 403000 both match it."""
        with steps.step(f"Send 1 {kind} email", timeout=15) as step:
            manifest = api.sender.send(kind, address=emi_run.address(), count=1)
            step.verify("sent", len(manifest.sent) == 1)
        with steps.step(f"Wait requiring markers ({form})", timeout=api.settings.wait_timeout + 5) as step:
            email = api.reader.wait_for_emails(emi_run.address(), CTA_OF[kind], timeout=step.remaining,
                                               settle_window=0.0, **_markers(form, emi_run, api.run_id))[0]
            step.verify("X-EMI-Test-Id is the case ID", email.email.test_id == emi_run.test_id)
            step.verify("X-EMI-Run-Id is the run", email.email.run_id == api.run_id.lower())

    @pytest.mark.emi(priority="P3")
    @pytest.mark.parametrize("kind", cases(403012, [(kind,) for kind in KINDS], name=lambda value: value[0]))
    def EMI_TOOL_FUNC_403012_other_run_marker_not_matched(
        self, api: EmiApi, steps: Steps, emi_run: EmiRun, kind: str,
    ) -> None:
        """Requiring another run's marker finds nothing, although the email is at the address."""
        with steps.step(f"Send 1 {kind} email", timeout=15) as step:
            step.verify("sent", len(api.sender.send(kind, address=emi_run.address(), count=1).sent) == 1)
        with steps.step("Wait requiring another run ID: nothing", timeout=NEGATIVE_WAIT_SEC + 10) as step:
            with pytest.raises(EmailWaitError) as caught:
                api.reader.wait_for_emails(emi_run.address(), CTA_OF[kind], timeout=NEGATIVE_WAIT_SEC,
                                           run_id=api.run_id + "-other", settle_window=0.0)
            step.verify("keyword is CASE_EMAIL_NOT_RECEIVED",
                        caught.value.primary == ReaderCaseKeyword.EMAIL_NOT_RECEIVED)

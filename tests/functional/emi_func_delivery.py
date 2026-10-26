# SPDX-FileCopyrightText: 2026 Aleksandr Polskiy
# SPDX-License-Identifier: Apache-2.0
"""Functional tests: delivery and CTA extraction per kind (design §13.3)."""

import pytest

from emi_api import EmiApi, EmiRun
from emi_harness import Steps
from emi_reader import Cta, assert_link_for


class TestEMIDelivery:
    """Each email kind arrives at the test's own address and yields the right link."""

    @pytest.mark.emi(priority="P0")
    def EMI_TOOL_FUNC_400001_waits_for_activation_email(self, api: EmiApi, steps: Steps, emi_run: EmiRun) -> None:
        """One activation email: the link received is the link sent, issued to this address."""
        with steps.precondition("Greenmail and test broker are running") as step:
            step.verify("health OK", api.health())
        with steps.step("Send 1 activation email", timeout=10) as step:
            manifest = api.sender.send("activation", address=emi_run.address(), count=1)
            step.verify("manifest lists 1 sent email", len(manifest.sent) == 1)
        with steps.step("Wait for the activation link", timeout=api.settings.wait_timeout + 5) as step:
            link = api.reader.wait_for_link(emi_run.address(), Cta.ACTIVATE_ACCOUNT, timeout=step.remaining)
            step.verify("link matches the sent one", link == manifest[0].link)
            assert_link_for(link, emi_run.address())

    @pytest.mark.emi(priority="P2", depends=["C400001"])
    def EMI_TOOL_FUNC_400002_waits_for_invitation_email(self, api: EmiApi, steps: Steps, emi_run: EmiRun) -> None:
        """One invitation email: the Accept invitation link, issued to this address."""
        with steps.step("Send 1 invitation email", timeout=10) as step:
            manifest = api.sender.send("invitation", address=emi_run.address(), count=1)
            step.verify("manifest lists 1 sent email", len(manifest.sent) == 1)
        with steps.step("Wait for the invitation link", timeout=api.settings.wait_timeout + 5) as step:
            link = api.reader.wait_for_link(emi_run.address(), Cta.ACCEPT_INVITATION, timeout=step.remaining)
            step.verify("link matches the sent one", link == manifest[0].link)
            assert_link_for(link, emi_run.address())

    @pytest.mark.emi(priority="P2", depends=["C400001"])
    def EMI_TOOL_FUNC_400003_picks_cta_among_other_kinds(self, api: EmiApi, steps: Steps, emi_run: EmiRun) -> None:
        """Generic, activation and invitation to one address: each CTA wait picks its own email."""
        with steps.step("Send one email of each kind", timeout=15) as step:
            sent = {kind: api.sender.send(kind, address=emi_run.address(), count=1)
                    for kind in ("generic", "activation", "invitation")}
            step.verify("three emails sent", all(len(manifest.sent) == 1 for manifest in sent.values()))
        with steps.step("Wait for both links", timeout=api.settings.wait_timeout + 5) as step:
            activation = api.reader.wait_for_link(emi_run.address(), Cta.ACTIVATE_ACCOUNT, timeout=step.remaining)
            invitation = api.reader.wait_for_link(emi_run.address(), Cta.ACCEPT_INVITATION, timeout=step.remaining)
            step.verify("activation link is the sent one", activation == sent["activation"][0].link)
            step.verify("invitation link is the sent one", invitation == sent["invitation"][0].link)

    @pytest.mark.emi(priority="P3", depends=["C400001"])
    def EMI_TOOL_FUNC_400004_counts_every_email(self, api: EmiApi, steps: Steps, emi_run: EmiRun) -> None:
        """Three activation emails to one address: exactly three arrive, each with its own link."""
        with steps.step("Send 3 activation emails", timeout=15) as step:
            manifest = api.sender.send("activation", address=emi_run.address(), count=3)
            step.verify("manifest lists 3 sent emails", len(manifest.sent) == 3)
        with steps.step("Wait for 3 emails", timeout=api.settings.wait_timeout + 5) as step:
            emails = api.reader.wait_for_emails(emi_run.address(), Cta.ACTIVATE_ACCOUNT, count=3,
                                                timeout=step.remaining)
            step.verify("3 distinct links", len({item.link for item in emails}) == 3)
            step.verify("links are the sent ones", {item.link for item in emails} == {entry.link
                                                                                      for entry in manifest.sent})

    @pytest.mark.emi(priority="P3")
    def EMI_TOOL_FUNC_400005_marker_carries_testrail_case(self, api: EmiApi, steps: Steps, emi_run: EmiRun) -> None:
        """Sent mail carries the TestRail case ID in its address and marker (AMB-63)."""
        with steps.step("Send 1 generic email", timeout=10) as step:
            manifest = api.sender.send("generic", address=emi_run.address(), count=1)
            step.verify("manifest lists 1 sent email", len(manifest.sent) == 1)
        with steps.step("Read it back by marker", timeout=api.settings.wait_timeout + 5) as step:
            email = api.reader.wait_for_emails(emi_run.address(), count=1, timeout=step.remaining,
                                               test_id="C400005", run_id=api.run_id)[0].email
            step.verify("X-EMI-Test-Id is the TestRail case ID", email.test_id == "C400005")
            step.verify("address carries the case ID", "+C400005_" in emi_run.address())

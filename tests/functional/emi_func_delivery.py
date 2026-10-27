# SPDX-FileCopyrightText: 2026 Aleksandr Polskiy
# SPDX-License-Identifier: Apache-2.0
"""Functional tests: delivery and CTA extraction per kind (design §13.3)."""

import threading

import pytest

from emi_api import EmiApi, EmiRun
from emi_harness import Steps
from emi_reader import Cta, LinkExtractionError, ReaderCaseKeyword, assert_link_for, extract_link
from emi_sender import Manifest
from tests.functional.catalog import CTA_OF


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

    @pytest.mark.emi(priority="P0")
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

    @pytest.mark.emi(priority="P0")
    def EMI_TOOL_FUNC_400006_generic_email_has_no_link(self, api: EmiApi, steps: Steps, emi_run: EmiRun) -> None:
        """One generic email arrives and carries no link: neither CTA can be extracted from it (AMB-68)."""
        with steps.step("Send 1 generic email", timeout=10) as step:
            manifest = api.sender.send("generic", address=emi_run.address(), count=1)
            step.verify("manifest lists 1 sent email", len(manifest.sent) == 1)
            step.verify("manifest lists no link", manifest[0].link is None)
        with steps.step("Wait for the email", timeout=api.settings.wait_timeout + 5) as step:
            received = api.reader.wait_for_emails(emi_run.address(), count=1, timeout=step.remaining)[0]
            step.verify("the received email is the sent one", received.email.message_id == manifest[0].message_id)
        with steps.step("Look for each CTA in it") as step:
            for cta in (Cta.ACTIVATE_ACCOUNT, Cta.ACCEPT_INVITATION):
                with pytest.raises(LinkExtractionError) as caught:
                    extract_link(received.email, cta)
                step.verify(f"no {cta} link (CASE_LINK_NOT_FOUND)",
                            caught.value.primary == ReaderCaseKeyword.LINK_NOT_FOUND)

    @pytest.mark.emi(priority="P0")
    def EMI_TOOL_FUNC_400007_activation_after_two_others(self, api: EmiApi, steps: Steps, emi_run: EmiRun) -> None:
        """Invitation and generic emails arrive first; the activation wait skips them for the later activation."""
        _wait_skips_others(api, steps, emi_run, "activation", ("invitation", "generic"))

    @pytest.mark.emi(priority="P0")
    def EMI_TOOL_FUNC_400008_invitation_after_two_others(self, api: EmiApi, steps: Steps, emi_run: EmiRun) -> None:
        """Activation and generic emails arrive first; the invitation wait skips them for the later invitation."""
        _wait_skips_others(api, steps, emi_run, "invitation", ("activation", "generic"))


def _wait_skips_others(api: EmiApi, steps: Steps, emi_run: EmiRun, wanted: str, others: tuple[str, str]) -> None:
    """Two non-matching emails are in the mailbox before the wait; the matching one is sent 2 s into it (AMB-68).

    The wait must not return either earlier email: it returns exactly the later one, with its own link.
    """
    with steps.step(f"Send 1 {others[0]} and 1 {others[1]} email", timeout=15) as step:
        early = [api.sender.send(kind, address=emi_run.address(), count=1) for kind in others]
        step.verify("two emails sent", all(len(manifest.sent) == 1 for manifest in early))
    with steps.step("Both have arrived before the wait", timeout=api.settings.wait_timeout + 5) as step:
        arrived = api.reader.wait_for_emails(emi_run.address(), count=2, timeout=step.remaining)
        step.verify("the two sent emails are in the mailbox",
                    sorted(item.message_id for item in arrived)
                    == sorted(manifest[0].message_id for manifest in early))
    sent: list[Manifest] = []
    sender = threading.Timer(2.0, lambda: sent.append(api.sender.send(wanted, address=emi_run.address())))
    steps.cleanup(f"stop the delayed {wanted} send", sender.cancel)
    with steps.step(f"Wait for the {wanted} link; it is sent 2 s later", timeout=api.settings.wait_timeout + 7) as step:
        sender.start()
        emails = api.reader.wait_for_emails(emi_run.address(), CTA_OF[wanted], timeout=step.remaining,
                                            settle_window=0.0)
        sender.join(timeout=10)
        step.verify(f"the delayed {wanted} send completed", len(sent) == 1 and len(sent[0].sent) == 1)
        step.verify("exactly one email returned", len(emails) == 1)
        step.verify(f"it is the later {wanted} email, not an earlier one",
                    emails[0].message_id == sent[0].sent[0].message_id)
        step.verify("its link is the one sent", emails[0].link == sent[0][0].link)
        assert_link_for(str(emails[0].link), emi_run.address())

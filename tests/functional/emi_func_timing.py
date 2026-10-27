# SPDX-FileCopyrightText: 2026 Aleksandr Polskiy
# SPDX-License-Identifier: Apache-2.0
"""Functional tests: mail that arrives while the wait is already running (design §5.4, §13.3)."""

import itertools
import threading

import pytest

from emi_api import EmiApi, EmiRun
from emi_harness import Steps
from emi_sender import Manifest
from tests.functional.catalog import CTA_OF, KINDS, cases

_DELAYS = (0.5, 1.0, 2.0, 3.0, 5.0)


class TestEMIArrivalDuringWait:
    """The wait starts first; the email is sent later and found by a later poll."""

    @pytest.mark.emi(priority="P2")
    @pytest.mark.parametrize(("kind", "delay"), cases(
        404000, list(itertools.product(KINDS, _DELAYS)), name=lambda value: f"{value[0]}-after-{value[1]:g}s"))
    def EMI_TOOL_FUNC_404000_arrives_mid_wait(  # pylint: disable=too-many-arguments
        self, api: EmiApi, steps: Steps, emi_run: EmiRun, kind: str, delay: float,
    ) -> None:
        """An email sent ``delay`` seconds after the wait began is found, and is the one sent."""
        sent: list[Manifest] = []
        sender = threading.Timer(delay, lambda: sent.append(api.sender.send(kind, address=emi_run.address())))
        steps.cleanup("stop the delayed send", sender.cancel)
        with steps.step(f"Start waiting, send 1 {kind} email after {delay:g} s",
                        timeout=api.settings.wait_timeout + delay + 5) as step:
            sender.start()
            emails = api.reader.wait_for_emails(emi_run.address(), CTA_OF[kind], timeout=step.remaining,
                                                settle_window=0.0)
            sender.join(timeout=10)
            step.verify("the delayed send completed", len(sent) == 1 and len(sent[0].sent) == 1)
            step.verify("the received email is the sent one", emails[0].message_id == sent[0].sent[0].message_id)

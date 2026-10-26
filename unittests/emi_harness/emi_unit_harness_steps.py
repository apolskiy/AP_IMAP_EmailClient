# SPDX-FileCopyrightText: 2026 Aleksandr Polskiy
# SPDX-License-Identifier: Apache-2.0
"""Unit tests: step model and outcome classification (design §9.2, §9.3)."""

from collections.abc import Iterator
from contextlib import contextmanager

import pytest

from emi_api import AddressInvalidError, ApiKeyword
from emi_harness import CaseFailure, CaseKeyword, CaseSkip, Status, Steps, classify
from emi_reader import EmailWaitError, ImapConnectionError, ReaderCaseKeyword, ReaderKeyword
from unittests.sims import SimClock


class SimHealth:
    """Object shaped like ``api.health()``."""

    def __init__(self, ready: bool) -> None:
        """Script readiness."""
        self.ready = ready
        self.keywords = ("", ()) if ready else ("GREENMAIL_NOT_READY", ("TEST_BROKER_UNAVAILABLE",))


class TestEMIHarnessSteps:
    """Numbering, verification, outcomes, time limits and cleanup."""

    def EMI_HARNESS_UNIT_200001_numbering_and_records(self) -> None:
        """PRE, Step 0 and S01.. are numbered automatically and recorded with their checks."""
        steps = Steps()
        with steps.precondition("environment") as step:
            step.verify("ready", True)
        with steps.setup("test data"):
            pass
        with steps.step("first") as step:
            step.verify("one", len("one") == 3)
        with steps.step("second"):
            pass
        assert [record.label for record in steps.records] == ["PRE", "S00", "S01", "S02"]
        assert steps.records[2].verifications == (("one", True),)
        assert all(record.status is Status.PASSED for record in steps.records)

    def EMI_HARNESS_UNIT_200002_failed_verification_fails_at_step(self) -> None:
        """A false verification fails the test at that step with CASE_VERIFICATION_FAILED."""
        steps = Steps()
        with steps.step("ok"):
            pass
        with pytest.raises(CaseFailure) as caught:
            with steps.step("check") as step:
                step.verify("link matches", False)
        assert (caught.value.primary, caught.value.step) == (CaseKeyword.VERIFICATION_FAILED, "S02")
        assert steps.failed_step is not None and steps.failed_step.status is Status.FAILED

    def EMI_HARNESS_UNIT_200003_failed_precondition_skips(self) -> None:
        """A failed PRE step is an infrastructure SKIP carrying the health keywords."""
        steps = Steps()
        with pytest.raises(CaseSkip) as caught:
            with steps.precondition("servers") as step:
                step.verify("health OK", SimHealth(False))
        assert caught.value.primary == "GREENMAIL_NOT_READY" and caught.value.infrastructure
        assert caught.value.secondary == ("TEST_BROKER_UNAVAILABLE",) and caught.value.step == "PRE"

    def EMI_HARNESS_UNIT_200004_typed_error_keeps_keywords(self) -> None:
        """A tool error inside a step fails the test with the tool's keyword."""
        steps = Steps()
        with pytest.raises(CaseFailure) as caught:
            with steps.step("wait"):
                raise EmailWaitError("none", ReaderCaseKeyword.EMAIL_NOT_RECEIVED)
        assert caught.value.primary == ReaderCaseKeyword.EMAIL_NOT_RECEIVED
        assert isinstance(caught.value.__cause__, EmailWaitError)

    def EMI_HARNESS_UNIT_200005_infrastructure_error_skips(self) -> None:
        """An environment failure in an action step is still a SKIP that stops the job."""
        steps = Steps()
        with pytest.raises(CaseSkip) as caught:
            with steps.step("wait"):
                raise ImapConnectionError("refused", ReaderKeyword.IMAP_CONNECTION_REFUSED, infrastructure=True)
        assert caught.value.primary == ReaderKeyword.IMAP_CONNECTION_REFUSED and caught.value.infrastructure

    @pytest.mark.parametrize("order", ["setup_twice", "pre_after_step", "setup_after_step"])
    def EMI_HARNESS_UNIT_200006_invalid_order_is_condition_error(self, order: str) -> None:
        """Steps out of order are a SKIP with CASE_CONDITION_INVALID_INPUT."""
        steps = Steps()
        with steps.setup("data") if order == "setup_twice" else steps.step("first"):
            pass
        start = {"setup_twice": steps.setup, "pre_after_step": steps.precondition, "setup_after_step": steps.setup}
        with pytest.raises(CaseSkip) as caught:
            start[order]("again")
        assert caught.value.primary == CaseKeyword.CONDITION_INVALID_INPUT

    def EMI_HARNESS_UNIT_200007_non_positive_limit_rejected(self) -> None:
        """A step time limit of 0 or less is a condition error."""
        with pytest.raises(CaseSkip) as caught:
            with Steps().step("bad", timeout=0):
                pass
        assert caught.value.primary == CaseKeyword.CONDITION_INVALID_INPUT

    def EMI_HARNESS_UNIT_200008_step_time_limit(self) -> None:
        """A step that returns after its limit fails with CASE_STEP_TIMEOUT; remaining shrinks meanwhile."""
        clock = SimClock()
        steps = Steps(step_timeout=5.0, clock=clock.now)
        with pytest.raises(CaseFailure) as caught:
            with steps.step("slow") as step:
                clock.advance(2.0)
                assert step.remaining == pytest.approx(3.0)
                clock.advance(4.0)
        assert caught.value.primary == CaseKeyword.STEP_TIMEOUT
        assert steps.records[0].primary == CaseKeyword.STEP_TIMEOUT

    def EMI_HARNESS_UNIT_200009_cleanups_always_run(self) -> None:
        """Cleanups run latest first; a failing one is CASE_CLEANUP_FAILED and the others still run."""
        steps = Steps()
        ran: list[str] = []

        def _broken() -> None:
            ran.append("broken")
            raise RuntimeError("cleanup broke")

        steps.cleanup("first", lambda: ran.append("first"))
        steps.cleanup("broken", _broken)
        steps.cleanup("last", lambda: ran.append("last"))
        assert steps.run_cleanups() == (CaseKeyword.CLEANUP_FAILED,)
        assert ran == ["last", "broken", "first"] and not steps.run_cleanups()

    def EMI_HARNESS_UNIT_200010_hook_wraps_each_step(self) -> None:
        """A report hook (e.g. allure.step) sees every step's title."""
        titles: list[str] = []

        @contextmanager
        def _hook(title: str) -> Iterator[None]:
            titles.append(title)
            yield

        steps = Steps(hook=_hook)
        with steps.precondition("env"):
            pass
        with steps.step("send"):
            pass
        assert titles == ["PRE env", "S01 send"]


class TestEMIHarnessClassify:
    """Every error becomes a verdict with keywords."""

    @pytest.mark.parametrize(("error", "status", "primary"), [
        (AssertionError("x"), Status.FAILED, "CASE_VERIFICATION_FAILED"),
        (KeyError("x"), Status.FAILED, "CASE_UNEXPECTED_ERROR"),
        (EmailWaitError("x", ReaderCaseKeyword.EMAIL_NOT_RECEIVED), Status.FAILED, "CASE_EMAIL_NOT_RECEIVED"),
        (ImapConnectionError("x", ReaderKeyword.IMAP_READ_TIMEOUT, infrastructure=True), Status.SKIPPED,
         "READER_IMAP_READ_TIMEOUT"),
        (AddressInvalidError("x", ApiKeyword.ADDRESS_INVALID), Status.FAILED, "API_ADDRESS_INVALID"),
        (CaseSkip("x", "CASE_CONDITION_NEGATIVE_COUNT"), Status.SKIPPED, "CASE_CONDITION_NEGATIVE_COUNT"),
    ])
    def EMI_HARNESS_UNIT_200015_classify(self, error: Exception, status: Status, primary: str) -> None:
        """Status and primary keyword per error type."""
        verdict = classify(error)
        assert (verdict.status, verdict.primary) == (status, primary)

    def EMI_HARNESS_UNIT_200016_condition_keyword_skips(self) -> None:
        """A typed error with a CASE_CONDITION_ keyword is a SKIP, not a failure."""
        error = EmailWaitError("bad count", "CASE_CONDITION_NEGATIVE_COUNT")
        assert classify(error).status is Status.SKIPPED

    def EMI_HARNESS_UNIT_200017_unexpected_error_names_its_class(self) -> None:
        """The class of an untyped error becomes the secondary keyword."""
        assert classify(ZeroDivisionError("boom")).secondary == ("ZeroDivisionError",)

# SPDX-FileCopyrightText: 2026 Aleksandr Polskiy
# SPDX-License-Identifier: Apache-2.0
"""Unit tests: run IDs, addresses, markers, settings and error typing (design §4, §8.2)."""

from datetime import UTC, datetime, timedelta, timezone

import pytest

from emi_api import (
    AddressInvalidError,
    ApiKeyword,
    ClientMode,
    EmiError,
    EmiRun,
    EmiSettings,
    RunId,
    build_address,
    case_id,
    keywords_of,
    marker_for,
    worker_number,
)
from emi_greenmail import GreenmailKeyword, GreenmailNotReadyError
from emi_reader import EmailWaitError, ReaderCaseKeyword
from emi_sender import InvalidInputError, Marker
from emi_test_broker import BrokerUnavailableError, TestBrokerKeyword

_RUN = "20261026t153012-w03-a1b2"


class TestEMIApiIdentity:
    """Run IDs and addresses follow design §4 exactly."""

    def EMI_API_UNIT_300001_run_id_shape(self) -> None:
        """Timestamp in UTC, two-digit worker, 4 hex; 24 characters."""
        moment = datetime(2026, 10, 26, 18, 30, 12, tzinfo=timezone(timedelta(hours=3)))
        run = RunId.new(3, now=moment, token="a1b2")
        assert str(run) == _RUN and len(str(run)) == 24

    def EMI_API_UNIT_300002_run_id_random_token(self) -> None:
        """Without a token the 4 hex characters are random."""
        tokens = {RunId.new(0, now=datetime(2026, 1, 1, tzinfo=UTC)).token for _index in range(20)}
        assert len(tokens) > 1 and all(len(token) == 4 for token in tokens)

    def EMI_API_UNIT_300003_run_id_parse_and_worker(self) -> None:
        """Parsing round-trips; another worker keeps the stamp and token."""
        run = RunId.parse(_RUN.upper())
        assert str(run) == _RUN
        assert str(run.for_worker(12)) == "20261026t153012-w12-a1b2"

    @pytest.mark.parametrize("text", ["", "20261026-w03-a1b2", "20261026t153012-w3-a1b2",
                                      "20261026t153012-w03-zzzz"])
    def EMI_API_UNIT_300004_run_id_rejects_bad_shapes(self, text: str) -> None:
        """Anything else is API_ADDRESS_INVALID."""
        with pytest.raises(AddressInvalidError) as caught:
            RunId.parse(text)
        assert caught.value.primary == ApiKeyword.ADDRESS_INVALID

    @pytest.mark.parametrize(("environ", "expected"), [({}, 0), ({"PYTEST_XDIST_WORKER": "gw7"}, 7),
                                                       ({"PYTEST_XDIST_WORKER": "master"}, 0)])
    def EMI_API_UNIT_300005_worker_number(self, environ: dict[str, str], expected: int) -> None:
        """pytest-xdist ``gw<n>`` gives the worker number."""
        assert worker_number(environ) == expected

    def EMI_API_UNIT_300010_address_example(self) -> None:
        """The design's example address carries the TestRail case ID; 35-character local part."""
        address = build_address("C100001", _RUN)
        assert address == "qa+C100001_20261026t153012-w03-a1b2@example.com"
        assert len(address.partition("@")[0]) == 35

    @pytest.mark.parametrize("test_id", ["400001", "C400001", "c400001", " C400001 "])
    def EMI_API_UNIT_300011_case_id_forms(self, test_id: str) -> None:
        """Every accepted form becomes the TestRail case ID; the rest of the address is lower-case."""
        assert case_id(test_id) == "C400001"
        assert build_address(test_id, "RUN_X", "QA@Example.COM") == "qa+C400001_run_x@example.com"

    @pytest.mark.parametrize(("test_id", "run_id"), [("C40000", _RUN), ("T400001", _RUN), ("C400001a", _RUN),
                                                     ("С400001", _RUN), ("тест1", _RUN), ("400 001", _RUN),
                                                     ("", _RUN), ("C400001", "прогон"), ("C400001", "run.1")])
    def EMI_API_UNIT_300012_address_rejects_bad_ids(self, test_id: str, run_id: str) -> None:
        """Not C + 6 digits (Cyrillic С included), Cyrillic, spaces or dots raise API_ADDRESS_INVALID."""
        with pytest.raises(AddressInvalidError) as caught:
            build_address(test_id, run_id)
        assert caught.value.primary == ApiKeyword.ADDRESS_INVALID

    def EMI_API_UNIT_300013_address_local_part_limit(self) -> None:
        """64 characters pass, 65 raise (RFC 5321)."""
        run_id = "r" * (64 - len("qa+C100001_"))
        assert len(build_address("C100001", run_id).partition("@")[0]) == 64
        with pytest.raises(AddressInvalidError) as caught:
            build_address("C1000001", run_id)
        assert caught.value.details["length"] == 65

    def EMI_API_UNIT_300017_address_slots(self) -> None:
        """Extra recipient slots keep the case ID and run; slot 0 is the test's own address; 0-99 only."""
        run = EmiRun("400001", _RUN)
        assert run.address(0) == run.address() == f"qa+C400001_{_RUN}@example.com"
        assert run.address(7) == f"qa+C400001_{_RUN}-r07@example.com"
        assert run.addresses(3) == tuple(f"qa+C400001_{_RUN}-r0{slot}@example.com" for slot in (1, 2, 3))
        assert len(set(run.addresses(99))) == 99 and run.address() not in run.addresses(99)
        assert marker_for(run.address(5)) == Marker(test_id="C400001", run_id=f"{_RUN}-r05")
        for slot in (-1, 100):
            with pytest.raises(AddressInvalidError) as caught:
                run.address(slot)
            assert caught.value.primary == ApiKeyword.ADDRESS_INVALID

    def EMI_API_UNIT_300014_address_bad_base_mailbox(self) -> None:
        """A base mailbox without a domain is refused."""
        with pytest.raises(AddressInvalidError):
            build_address("C100001", _RUN, "qa")

    def EMI_API_UNIT_300015_marker_from_address(self) -> None:
        """A test address implies its marker in TestRail form, whatever the case; other addresses imply none."""
        expected = Marker(test_id="C400001", run_id=_RUN)
        assert marker_for("qa+C400001_" + _RUN + "@example.com") == expected
        assert marker_for(("qa+C400001_" + _RUN + "@example.com").lower()) == expected
        assert marker_for("someone@example.com") is None

    def EMI_API_UNIT_300016_emi_run(self) -> None:
        """EmiRun stores the TestRail case ID and builds its address and marker."""
        run = EmiRun("400001", _RUN, "qa@example.com")
        assert run.test_id == "C400001"
        assert run.address() == "qa+C400001_" + _RUN + "@example.com"
        assert run.marker == marker_for(run.address()) and run.marker.test_id == "C400001"
        with pytest.raises(AddressInvalidError):
            EmiRun("тест", _RUN)


class TestEMIApiSettings:
    """Settings come from the environment and stay redacted."""

    def EMI_API_UNIT_300020_settings_from_env(self) -> None:
        """Mode and tool settings come from EMI_* variables; overrides win."""
        settings = EmiSettings.from_env({"EMI_CLIENT_MODE": "InProcess", "EMI_IMAP_HOST": "mail.local",
                                         "EMI_TEST_BROKER_PORT": "9025"}, wait_timeout=5.0)
        assert settings.mode is ClientMode.INPROCESS and settings.wait_timeout == 5.0
        assert settings.imap.host == "mail.local" and settings.broker.port == 9025

    def EMI_API_UNIT_300021_settings_reject_bad_values(self) -> None:
        """An unknown mode or an out-of-range timeout is refused."""
        with pytest.raises(ValueError):
            EmiSettings.from_env({"EMI_CLIENT_MODE": "remote"})
        with pytest.raises(ValueError):
            EmiSettings(wait_timeout=0.5)

    def EMI_API_UNIT_300022_settings_describe_redacts(self) -> None:
        """No password appears in the description."""
        settings = EmiSettings.from_env({"EMI_IMAP_PASSWORD": "s3cret-imap", "EMI_SMTP_PASSWORD": "s3cret-smtp"})
        text = repr(settings.describe())
        assert "s3cret" not in text and settings.describe()["mode"] == "service"

    def EMI_API_UNIT_300023_greenmail_defaults_follow_mail_ports(self) -> None:
        """Without explicit Greenmail settings the IMAP host and both mail ports are probed."""
        settings = EmiSettings.from_env({"EMI_IMAP_HOST": "gm.local", "EMI_IMAP_PORT": "4143",
                                         "EMI_SMTP_PORT": "4025"})
        greenmail = settings.greenmail_settings
        assert greenmail.host == "gm.local" and dict(greenmail.probe_ports) == {"smtp": 4025, "imap": 4143}


class TestEMIApiErrors:
    """``except EmiError`` catches every tool's typed error and keeps its keywords."""

    @pytest.mark.parametrize(("error", "primary"), [
        (EmailWaitError("none", ReaderCaseKeyword.EMAIL_NOT_RECEIVED), "CASE_EMAIL_NOT_RECEIVED"),
        (InvalidInputError("bad", "CASE_CONDITION_INVALID_INPUT"), "CASE_CONDITION_INVALID_INPUT"),
        (BrokerUnavailableError("down", TestBrokerKeyword.UNAVAILABLE, infrastructure=True),
         "TEST_BROKER_UNAVAILABLE"),
        (GreenmailNotReadyError("gm", GreenmailKeyword.NOT_READY, infrastructure=True), "GREENMAIL_NOT_READY"),
        (AddressInvalidError("addr", ApiKeyword.ADDRESS_INVALID), "API_ADDRESS_INVALID"),
    ])
    def EMI_API_UNIT_300030_emi_error_catches_every_tool(self, error: Exception, primary: str) -> None:
        """Every family is an EmiError and reports its primary keyword."""
        with pytest.raises(EmiError):
            raise error
        assert keywords_of(error)[0] == primary

    def EMI_API_UNIT_300031_keywords_of_plain_error(self) -> None:
        """An untyped error has no keywords."""
        assert keywords_of(RuntimeError("boom")) == ("", ())

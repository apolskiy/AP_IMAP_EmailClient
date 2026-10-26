# SPDX-FileCopyrightText: 2026 Aleksandr Polskiy
# SPDX-License-Identifier: Apache-2.0
"""Unit tests: option table, results files, calibration, session setup and the keyword catalog (design §3, §9)."""

import argparse
import json
import re
from enum import StrEnum
from pathlib import Path

import pytest

from emi_api import EmiSettings, Health, Problem
from emi_api.keywords import ApiKeyword
from emi_greenmail import GreenmailKeyword
from emi_harness import CaseSkip, HarnessKeyword, Status, StepRecord, TestOutcome
from emi_harness.keywords import CaseKeyword, JobKeyword
from emi_harness.options import OPTIONS, Source, add_arguments, add_pytest_options, pass_through, resolve
from emi_harness.results import ResultsWriter, calibrate, job_dir, load_results, run_folder
from emi_harness.session import PROBE_CASE, check_health, send_probe
from emi_reader import EmailWaitError, ReaderCaseKeyword, ReaderKeyword
from emi_reader.keywords import ReaderCaseKeyword as ReaderCases
from emi_sender import Manifest, ManifestEntry
from emi_sender.keywords import SenderCaseKeyword, SenderKeyword
from emi_test_broker import TestBrokerKeyword

_CATALOG = Path(__file__).resolve().parents[2] / "docs" / "keywords.md"


class TestEMIHarnessOptions:
    """One table, precedence with sources, redaction, parity (design §3)."""

    def EMI_HARNESS_UNIT_200060_precedence_and_sources(self) -> None:
        """argument > CLI > env > .env > default, each value reported with its source."""
        resolved = resolve(cli={"imap_host": "cli.host", "wait_timeout": "30"},
                           environ={"EMI_IMAP_HOST": "env.host", "EMI_SMTP_HOST": "env.smtp"},
                           dotenv={"EMI_SMTP_HOST": "dotenv.smtp", "EMI_IMAP_USER": "dotenv.user"},
                           arguments={"wait_timeout": 12.0})
        assert (resolved["imap_host"], resolved.sources["imap_host"]) == ("cli.host", Source.CLI)
        assert (resolved["smtp_host"], resolved.sources["smtp_host"]) == ("env.smtp", Source.ENV)
        assert (resolved["imap_user"], resolved.sources["imap_user"]) == ("dotenv.user", Source.DOTENV)
        assert (resolved["wait_timeout"], resolved.sources["wait_timeout"]) == (12.0, Source.ARGUMENT)
        assert (resolved["max_connections"], resolved.sources["max_connections"]) == (10, Source.DEFAULT)

    @pytest.mark.parametrize(("environ", "message"), [({"EMI_MAX_CONNECTIONS": "0"}, "1-100"),
                                                       ({"EMI_WAIT_TIMEOUT": "soon"}, "number"),
                                                       ({"EMI_CLIENT_MODE": "remote"}, "one of"),
                                                       ({"EMI_PROBE": "maybe"}, "on/off")])
    def EMI_HARNESS_UNIT_200061_values_validated(self, environ: dict[str, str], message: str) -> None:
        """Out-of-range or mistyped values are refused, naming the flag and the variable."""
        with pytest.raises(ValueError, match=message):
            resolve(environ=environ)

    def EMI_HARNESS_UNIT_200062_secrets_never_from_cli_and_redacted(self) -> None:
        """A password given on the command line is ignored; a set password is shown as ***."""
        resolved = resolve(cli={"imap_password": "from-cli"}, environ={"EMI_IMAP_PASSWORD": "s3cret-imap"})
        assert resolved["imap_password"] == "s3cret-imap" and resolved.sources["imap_password"] is Source.ENV
        shown = json.dumps(resolved.describe())
        assert "s3cret" not in shown and "from-cli" not in shown
        assert resolved.describe()["imap_password"] == {"value": "***", "source": "env", "env": "EMI_IMAP_PASSWORD"}

    def EMI_HARNESS_UNIT_200063_cli_and_pytest_options_match(self) -> None:
        """The harness CLI and the pytest plugin expose exactly the same --emi-* settings (parity)."""
        parser = argparse.ArgumentParser()
        add_arguments(parser)
        cli_flags = {flag for action in parser._actions for flag in action.option_strings  # pylint: disable=protected-access
                     if flag.startswith("--emi-")}
        pytest_flags: set[str] = set()
        add_pytest_options(lambda flag, **_kwargs: pytest_flags.add(flag))
        expected = {option.flag for option in OPTIONS if not option.secret}
        assert cli_flags == pytest_flags == expected
        assert not {option.flag for option in OPTIONS if option.secret} & cli_flags

    def EMI_HARNESS_UNIT_200064_pass_through(self) -> None:
        """Given --emi-* flags are passed to pytest unchanged; absent ones are not invented."""
        namespace = argparse.Namespace(emi_imap_host="h", emi_max_connections="5", emi_smtp_host=None)
        assert pass_through(namespace) == ["--emi-imap-host", "h", "--emi-max-connections", "5"]

    def EMI_HARNESS_UNIT_200065_settings_built(self) -> None:
        """The options become API settings: mode, folders, ports, SMTP login only with a set password."""
        settings: EmiSettings = resolve(environ={"EMI_CLIENT_MODE": "inprocess", "EMI_IMAP_FOLDERS": "INBOX, Spam",
                                                 "EMI_IMAP_PORT": "4143", "EMI_TEST_BROKER_PORT": "9025",
                                                 "EMI_SMTP_PASSWORD": "pw"}).emi_settings()
        assert str(settings.mode) == "inprocess" and settings.imap.folders == ("INBOX", "Spam")
        assert settings.broker.port == 9025 and settings.smtp.smtp_login
        assert settings.greenmail_settings.probe_ports["imap"] == 4143
        assert not resolve().emi_settings().smtp.smtp_login


class TestEMIHarnessResults:
    """Results files and calibration (design §9.8, §10)."""

    def EMI_HARNESS_UNIT_200070_run_folder(self, tmp_path: Path) -> None:
        """Every worker of a run shares one folder per job."""
        assert run_folder("20261026t153012-w03-a1b2") == "20261026t153012-a1b2"
        assert job_dir(tmp_path, "20261026t153012-w07-a1b2", "P1") == tmp_path / "20261026t153012-a1b2" / "P1"

    def EMI_HARNESS_UNIT_200071_results_round_trip(self, tmp_path: Path) -> None:
        """Results written by several workers read back unchanged, steps included; no file until a write."""
        outcome = TestOutcome(nodeid="n1", case_id="C400001", priority="P0", status=Status.FAILED,
                              primary="CASE_X", secondary=("A",), step="S02", duration=1.5, job="P0",
                              steps=(StepRecord("S01", "send", Status.PASSED, 0.1, (("sent", True),)),))
        writer = ResultsWriter(tmp_path / "job", "gw0")
        assert not writer.path.parent.exists()
        writer.write(outcome)
        ResultsWriter(tmp_path / "job", "gw1").write(outcome)
        assert load_results(tmp_path) == [outcome, outcome]

    def EMI_HARNESS_UNIT_200072_calibration(self) -> None:
        """Percentiles of passing tests and steps; limit = p99 x 2 rounded up."""
        outcomes = [TestOutcome(nodeid=f"n{number}", case_id="", priority="P2", status=Status.PASSED,
                                duration=float(number), steps=(StepRecord("S01", "wait", Status.PASSED, 0.5),))
                    for number in range(1, 101)]
        outcomes.append(TestOutcome(nodeid="bad", case_id="", priority="P2", status=Status.FAILED, duration=999.0))
        report = calibrate(outcomes)
        assert report["tests"]["p50"] == 50.0 and report["tests"]["p99"] == 99.0
        assert report["tests"]["suggested_limit"] == 198.0 and report["steps"]["wait"]["suggested_limit"] == 1.0


class SimProbeApi:
    """API double for the session probe."""

    def __init__(self, wait_error: Exception | None = None, failed: bool = False) -> None:
        """Script the probe."""
        self.run_id = "20261026t153012-w00-a1b2"
        self.settings = EmiSettings()
        self.sender = self
        self.reader = self
        self.wait_error = wait_error
        self.failed = failed
        self.batches: list[object] = []

    def send_batch(self, request: object, **_options: object) -> Manifest:
        """Record the batch; deliver or fail."""
        self.batches.append(request)
        status = "failed" if self.failed else "sent"
        keyword = SenderKeyword.SMTP_CONNECTION_REFUSED if self.failed else ""
        entries = [ManifestEntry(index=0, address="a", field="to", kind="activation", status=status,
                                 keyword=keyword)]
        return Manifest(entries=entries, seed=1)

    def wait_for_emails(self, address: str, **options: object) -> list[object]:
        """Raise the scripted error or return the emails."""
        del address
        if self.wait_error is not None:
            raise self.wait_error
        return [object()] * int(str(options["count"]))


class TestEMIHarnessSession:
    """Session setup (design §9.6)."""

    def EMI_HARNESS_UNIT_200080_health_problem_skips(self) -> None:
        """Not ready: infrastructure skip with the health keywords and every problem named."""
        health = Health(mode="service", problems=(Problem("greenmail", GreenmailKeyword.NOT_READY, (), "down"),
                                                  Problem("broker", TestBrokerKeyword.UNAVAILABLE, (), "gone")))
        with pytest.raises(CaseSkip) as caught:
            check_health(health)
        assert caught.value.infrastructure and caught.value.primary == GreenmailKeyword.NOT_READY
        assert caught.value.secondary == (TestBrokerKeyword.UNAVAILABLE,) and "broker: gone" in str(caught.value)
        check_health(Health(mode="service"))

    def EMI_HARNESS_UNIT_200081_probe_sends_each_kind(self) -> None:
        """One email per kind to qa+C000000_<run id>, waited for."""
        api = SimProbeApi()
        send_probe(api, 15.0)  # type: ignore[arg-type]
        request = api.batches[0]
        assert getattr(request, "addresses")[0].startswith(f"qa+{PROBE_CASE}_")
        assert set(getattr(request, "counts")) == {"activation", "invitation", "generic"}

    def EMI_HARNESS_UNIT_200082_probe_not_received(self) -> None:
        """A probe that never arrives: HARNESS_PROBE_EMAIL_NOT_RECEIVED, the reader's keyword secondary."""
        api = SimProbeApi(EmailWaitError("none", ReaderCaseKeyword.EMAIL_NOT_RECEIVED))
        with pytest.raises(CaseSkip) as caught:
            send_probe(api, 15.0)  # type: ignore[arg-type]
        assert caught.value.primary == HarnessKeyword.PROBE_EMAIL_NOT_RECEIVED and caught.value.infrastructure
        assert caught.value.secondary[0] == ReaderCaseKeyword.EMAIL_NOT_RECEIVED

    def EMI_HARNESS_UNIT_200083_probe_not_delivered(self) -> None:
        """A probe the sender could not deliver keeps the sender's keyword."""
        with pytest.raises(CaseSkip) as caught:
            send_probe(SimProbeApi(failed=True), 15.0)  # type: ignore[arg-type]
        assert caught.value.primary == SenderKeyword.SMTP_CONNECTION_REFUSED and caught.value.infrastructure


class TestEMIHarnessCatalog:
    """Every keyword in code is in docs/keywords.md (design §13.1)."""

    def EMI_HARNESS_UNIT_200090_catalog_complete(self) -> None:
        """No keyword enum member is missing from the catalog."""
        catalog = set(re.findall(r"`([A-Z][A-Z0-9_]+)`", _CATALOG.read_text(encoding="utf-8")))
        families: list[type[StrEnum]] = [ReaderKeyword, ReaderCases, SenderKeyword, SenderCaseKeyword,
                                         TestBrokerKeyword, GreenmailKeyword, ApiKeyword, HarnessKeyword,
                                         CaseKeyword, JobKeyword]
        missing = sorted(str(member) for family in families for member in family if str(member) not in catalog)
        assert not missing, f"missing from docs/keywords.md: {missing}"

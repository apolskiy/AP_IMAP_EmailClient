# SPDX-FileCopyrightText: 2026 Aleksandr Polskiy
# SPDX-License-Identifier: Apache-2.0
"""Unit tests: delivery faults, retries, manifest and exit codes (design §6.4, §6.6)."""

import smtplib
import socket
import ssl
import subprocess
import sys
from pathlib import Path

import pytest

from emi_sender import BatchRequest, Manifest, ManifestEntry, Marker, Preset, Sender, SenderKeyword, SenderSettings
from emi_sender.cli import main
from emi_sender.delivery import classify_connect, classify_send
from emi_test_broker import TestBrokerKeyword
from emi_test_broker.errors import BrokerUnavailableError
from unittests.emi_sender.sender_sims import SimOutbox, dropped, refused, rejected
from unittests.sims import SimClock

_ADDRESS = "qa+400200_run-a1@example.com"


def _sender(outbox: SimOutbox, clock: SimClock | None = None, password: str = "password") -> Sender:
    """Sender wired to simulations."""
    clock = clock or SimClock()
    return Sender(SenderSettings(password=password), delivery_factory=outbox.factory, clock=clock.now,
                  sleeper=clock.sleep)


def _wrapped(cause: OSError) -> smtplib.SMTPServerDisconnected:
    """How smtplib reports a failed socket write: SMTPServerDisconnected with the OSError as context."""
    error = smtplib.SMTPServerDisconnected("Server not connected")
    error.__context__ = cause
    return error


class TestEMISenderClassification:
    """Every SMTP failure gets its keyword and retry decision."""

    @pytest.mark.parametrize(("error", "keyword"), [
        (ConnectionRefusedError(10061, "refused"), SenderKeyword.SMTP_CONNECTION_REFUSED),
        (socket.gaierror(11001, "no such host"), SenderKeyword.SMTP_SERVER_UNREACHABLE),
        (TimeoutError("timed out"), SenderKeyword.SMTP_CONNECTION_TIMEOUT),
        (ssl.SSLError("handshake"), SenderKeyword.SMTP_TLS_HANDSHAKE_FAILED),
        (smtplib.SMTPAuthenticationError(535, b"bad credentials"), SenderKeyword.SMTP_AUTH_FAILED),
    ])
    @pytest.mark.network
    def EMI_SENDER_UNIT_110040_connect_faults_are_fatal(self, error: BaseException, keyword: str) -> None:
        """Server down, DNS, TLS and auth faults are never retried."""
        classified = classify_connect(error, "127.0.0.1:3025")
        assert (classified.primary, classified.retryable, classified.infrastructure) == (keyword, False, True)

    @pytest.mark.parametrize(("error", "keyword", "retryable"), [
        (smtplib.SMTPRecipientsRefused({"qa@example.com": (550, b"no such user")}),
         SenderKeyword.SMTP_RECIPIENT_REJECTED, False),
        (smtplib.SMTPDataError(554, b"rejected"), SenderKeyword.SMTP_MESSAGE_REJECTED, False),
        (smtplib.SMTPDataError(451, b"try later"), SenderKeyword.SMTP_MESSAGE_REJECTED, True),
        (TimeoutError("timed out"), SenderKeyword.SMTP_READ_TIMEOUT, True),
        (_wrapped(BrokenPipeError(32, "broken pipe")), SenderKeyword.SMTP_SOCKET_WRITE_FAILED, True),
        (_wrapped(ConnectionAbortedError(10053, "aborted")), SenderKeyword.SMTP_SOCKET_WRITE_FAILED, True),
        (smtplib.SMTPServerDisconnected("closed"), SenderKeyword.SMTP_CONNECTION_DROPPED, True),
    ])
    @pytest.mark.network
    def EMI_SENDER_UNIT_110041_send_faults(self, error: BaseException, keyword: str, retryable: bool) -> None:
        """Rejections are not retried (except 4xx); mid-session network faults are."""
        classified = classify_send(error, "127.0.0.1:3025")
        assert (classified.primary, classified.retryable) == (keyword, retryable)


class TestEMISenderBatches:
    """Sending, retries, failures and the manifest."""

    def EMI_SENDER_UNIT_110050_send_returns_manifest(self) -> None:
        """All sent: exit code 0, one entry per email with link and Message-ID."""
        outbox = SimOutbox()
        manifest = _sender(outbox).send("activation", _ADDRESS, 2, marker=Marker(test_id="400200"))
        assert manifest.exit_code == 0 and len(manifest) == 2
        assert all(entry.status == "sent" and entry.link and entry.message_id for entry in manifest.entries)
        assert len(outbox.delivered) == 2

    @pytest.mark.network
    def EMI_SENDER_UNIT_110051_healable_fault_is_retried(self) -> None:
        """A dropped connection is retried with backoff and the email is still sent."""
        outbox = SimOutbox()
        outbox.script = [dropped()]
        clock = SimClock()
        manifest = _sender(outbox, clock).send("activation", _ADDRESS)
        assert manifest[0].status == "sent" and manifest[0].attempts == 2
        assert clock.sleeps == [0.5]

    @pytest.mark.network
    def EMI_SENDER_UNIT_110052_server_down_is_exit_3_and_stops_the_lane(self) -> None:
        """Refused: not retried, the rest of the lane is not attempted, exit code 3."""
        outbox = SimOutbox()
        outbox.script = [refused()]
        manifest = _sender(outbox).send("activation", _ADDRESS, 3)
        assert manifest.exit_code == 3
        assert [entry.keyword for entry in manifest.entries] == [SenderKeyword.SMTP_CONNECTION_REFUSED] * 3
        assert manifest.keywords[0] == SenderKeyword.SMTP_CONNECTION_REFUSED
        assert not outbox.delivered

    def EMI_SENDER_UNIT_110053_rejection_is_partial_failure(self) -> None:
        """One rejected email out of three: exit code 1, PARTIAL_FAILURE with the cause as secondary."""
        outbox = SimOutbox()
        outbox.script = [rejected()]
        manifest = _sender(outbox).send("activation", _ADDRESS, 3)
        assert manifest.exit_code == 1
        assert manifest.keywords == (SenderKeyword.PARTIAL_FAILURE, (SenderKeyword.SMTP_RECIPIENT_REJECTED,))
        assert len(manifest.sent) == 2

    def EMI_SENDER_UNIT_110054_concurrency_uses_own_connections(self) -> None:
        """Concurrency N opens N connections and keeps the planned order in the manifest."""
        outbox = SimOutbox()
        addresses = tuple(f"qa+400201_run-{index}@example.com" for index in range(5))
        request = BatchRequest(addresses=addresses, counts={"activation": 5, "generic": 5}, preset=Preset.EVEN_5,
                               concurrency=3, seed=5)
        manifest = _sender(outbox).send_batch(request)
        assert outbox.connections == 3
        assert [entry.index for entry in manifest.entries] == list(range(10))

    def EMI_SENDER_UNIT_110055_batch_budget_exceeded(self) -> None:
        """Emails left when the batch budget runs out are reported with BATCH_TIMEOUT."""
        outbox = SimOutbox()
        clock = SimClock()

        def _slow_factory(settings: SenderSettings) -> object:
            clock.advance(11.0)  # connecting took longer than the whole 10 s + 0.05 s/email budget
            return outbox.factory(settings)

        sender = Sender(SenderSettings(), delivery_factory=_slow_factory, clock=clock.now,  # type: ignore[arg-type]
                        sleeper=clock.sleep)
        manifest = sender.send("activation", _ADDRESS, 2)
        assert {entry.keyword for entry in manifest.entries} == {SenderKeyword.BATCH_TIMEOUT}

    def EMI_SENDER_UNIT_110056_password_never_in_manifest(self) -> None:
        """The manifest describes the settings with the password redacted."""
        manifest = _sender(SimOutbox(), password="Sup3r-secret").send("generic", _ADDRESS)
        assert "Sup3r-secret" not in manifest.to_json()


class TestEMISenderCli:
    """Exit codes: 0 sent, 1 partial, 2 invalid input, 3 unreachable."""

    def EMI_SENDER_UNIT_110060_cli_sends_and_exits_0(self, capsys: pytest.CaptureFixture[str]) -> None:
        """A valid command prints the manifest and exits 0."""
        code = main(["--to", _ADDRESS, "--activation", "1", "--test-id", "400200"], sender=_sender(SimOutbox()))
        assert code == 0 and '"exit_code": 0' in capsys.readouterr().out

    def EMI_SENDER_UNIT_110061_non_integer_count_exits_2(self) -> None:
        """argparse rejects a non-integer count with exit code 2."""
        with pytest.raises(SystemExit) as caught:
            main(["--to", _ADDRESS, "--activation", "abc"], sender=_sender(SimOutbox()))
        assert caught.value.code == 2

    def EMI_SENDER_UNIT_110062_negative_count_exits_0_and_sends_nothing(self) -> None:
        """A negative count sends nothing, is logged, and is not an error."""
        outbox = SimOutbox()
        assert main(["--to", _ADDRESS, "--activation", "-2"], sender=_sender(outbox)) == 0
        assert not outbox.delivered

    @pytest.mark.parametrize("bad_args", [["--test-id", "тест"], ["--concurrency", "0"], ["--preset", "even_5"]])
    def EMI_SENDER_UNIT_110063_invalid_input_exits_2(self, bad_args: list[str]) -> None:
        """Cyrillic test ID, out-of-range concurrency, too few addresses for the preset."""
        assert main(["--to", _ADDRESS, "--activation", "1", *bad_args], sender=_sender(SimOutbox())) == 2

    @pytest.mark.network
    def EMI_SENDER_UNIT_110064_unreachable_exits_3(self) -> None:
        """Server down → exit code 3."""
        outbox = SimOutbox()
        outbox.script = [refused()]
        assert main(["--to", _ADDRESS, "--activation", "1"], sender=_sender(outbox)) == 3

    def EMI_SENDER_UNIT_110070_via_broker_sends_through_the_broker(self) -> None:
        """``--via-broker`` hands the batch to the broker client; without it the sender delivers itself
        (AMB-84). A broker error is exit 3, like an unreachable server."""
        outbox, broker = SimOutbox(), SimBroker()
        assert main(["--to", _ADDRESS, "--activation", "1", "--via-broker"], sender=_sender(outbox),
                    broker=broker) == 0
        assert len(broker.batches) == 1 and not outbox.delivered
        assert main(["--to", _ADDRESS, "--activation", "1"], sender=_sender(outbox), broker=broker) == 0
        assert len(broker.batches) == 1 and outbox.delivered
        broker.error = BrokerUnavailableError("no broker", TestBrokerKeyword.UNAVAILABLE, infrastructure=True)
        assert main(["--to", _ADDRESS, "--activation", "1", "--via-broker"], broker=broker) == 3

    def EMI_SENDER_UNIT_110071_sender_runs_without_the_broker(self) -> None:
        """Importing and running the sender CLI without ``--via-broker`` never imports the broker package."""
        program = "import sys, emi_sender.cli; print(any(name.startswith('emi_test_broker') for name in sys.modules))"
        result = subprocess.run([sys.executable, "-c", program], capture_output=True, text=True, check=True,
                                cwd=Path(__file__).resolve().parents[2], timeout=60)
        assert result.stdout.strip() == "False"


class SimBroker:  # pylint: disable=too-few-public-methods
    """Broker client double: records batches or raises a scripted error."""

    def __init__(self) -> None:
        """Nothing sent yet."""
        self.batches: list[BatchRequest] = []
        self.error: Exception | None = None

    def send_batch(self, request: BatchRequest, *, marker: Marker | None = None) -> Manifest:
        """Record the batch and report every email sent."""
        del marker
        if self.error is not None:
            raise self.error
        self.batches.append(request)
        return Manifest(entries=[ManifestEntry(index=0, address=_ADDRESS, field="to", kind="activation",
                                               status="sent")], seed=1)

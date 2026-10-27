# SPDX-FileCopyrightText: 2026 Aleksandr Polskiy
# SPDX-License-Identifier: Apache-2.0
"""Unit tests: wire format, HTTP server and client over real loopback sockets, log, CLI (design §7)."""

import json
import logging
import threading
from collections.abc import Iterator
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from emi_greenmail import GreenmailKeyword, GreenmailNotReadyError
from emi_reader import Cta, EmailWaitError, ImapConnectionError, ImapSettings, ReaderCaseKeyword, ReaderKeyword
from emi_sender import BatchRequest, InvalidInputError, Marker, Preset, SenderCaseKeyword, SenderSettings
from emi_test_broker import (
    BrokerClient,
    BrokerService,
    BrokerSettings,
    BrokerUnavailableError,
    LeaseKeeper,
    RemoteError,
    TestBrokerKeyword,
)
from emi_test_broker.broker_log import IsoFormatter, debug_entries, log_path, open_log_handler
from emi_test_broker.cli import build_parser, main
from emi_test_broker.client import START_ERRORS_LOG, spawn_command
from emi_test_broker.settings import settings_from_json, settings_to_json
from emi_test_broker.codec import (
    batch_request_from_json,
    batch_request_to_json,
    error_from_json,
    error_to_json,
    received_from_json,
    received_to_json,
)
from emi_test_broker.server import bind, serve
from unittests.emi_test_broker.broker_sims import LINK, SimMailboxes, SimSender, free_port, received, start_served
from unittests.sims import SimClock

_ADDRESS = "qa+400001_run-1@example.com"


@pytest.fixture(name="running")
def _running(tmp_path: Path) -> Iterator[tuple[BrokerClient, SimMailboxes]]:
    """A real broker on a free loopback port, wired to simulations; stopped afterwards."""
    settings, _service, mailboxes, thread = start_served(tmp_path)
    client = BrokerClient(settings, spawner=lambda _settings: None)
    yield client, mailboxes
    client.stop()
    thread.join(timeout=5)


class TestEMIBrokerWire:
    """Typed errors and results survive the trip unchanged."""

    @pytest.mark.parametrize("error", [
        EmailWaitError("none", ReaderCaseKeyword.EMAIL_NOT_RECEIVED, details={"found": 0}),
        ImapConnectionError("down", ReaderKeyword.IMAP_CONNECTION_REFUSED, infrastructure=True),
        InvalidInputError("bad", "CASE_CONDITION_INVALID_INPUT"),
        GreenmailNotReadyError("gm", GreenmailKeyword.IMAP_CONNECTION_FAILURE,
                               secondary=(GreenmailKeyword.NOT_READY,), infrastructure=True),
    ])
    def EMI_TEST_BROKER_UNIT_130040_errors_round_trip(self, error: Exception) -> None:
        """Same class, keywords, flags and details on the client."""
        rebuilt = error_from_json(json.loads(json.dumps(error_to_json(error))))
        assert type(rebuilt) is type(error)
        for name in ("primary", "secondary", "infrastructure", "details"):
            assert getattr(rebuilt, name, None) == getattr(error, name, None)

    def EMI_TEST_BROKER_UNIT_130041_unknown_error_type_kept_as_remote(self) -> None:
        """A class the client does not know becomes RemoteError with its keywords."""
        rebuilt = error_from_json({"type": "Strange", "message": "x", "primary": "SOMETHING"})
        assert isinstance(rebuilt, RemoteError) and rebuilt.primary == "SOMETHING"

    def EMI_TEST_BROKER_UNIT_130042_received_email_round_trip(self) -> None:
        """Every field of a received email survives."""
        original = received(_ADDRESS)
        assert received_from_json(json.loads(json.dumps(received_to_json(original)))) == original

    def EMI_TEST_BROKER_UNIT_130043_batch_request_round_trip(self) -> None:
        """Batch requests survive, presets and seeds included."""
        request = BatchRequest(addresses=(_ADDRESS,) * 5, counts={"activation": 3}, preset=Preset.EVEN_5, seed=4,
                               concurrency=2)
        assert batch_request_from_json(json.loads(json.dumps(batch_request_to_json(request)))) == request


class TestEMIBrokerHttp:
    """Server and client over real loopback sockets."""

    def EMI_TEST_BROKER_UNIT_130050_health_and_ensure_running(
        self, running: tuple[BrokerClient, SimMailboxes]
    ) -> None:
        """A matching broker is reused; no start attempt."""
        client, _mailboxes = running
        assert client.ensure_running()["service"] == "emi_test_broker"

    def EMI_TEST_BROKER_UNIT_130051_send_and_wait_through_http(
        self, running: tuple[BrokerClient, SimMailboxes]
    ) -> None:
        """Send returns a manifest; waits return a link and emails."""
        client, _mailboxes = running
        manifest = client.send("activation", _ADDRESS, marker=Marker(test_id="400001", run_id="run-1"))
        assert manifest.exit_code == 0 and manifest.run_id == "run-1"
        assert client.wait_for_link(_ADDRESS, Cta.ACTIVATE_ACCOUNT, timeout=5) == LINK
        assert client.wait_for_emails(_ADDRESS, count=2, timeout=5)[1].uid == 2

    def EMI_TEST_BROKER_UNIT_130052_reader_error_arrives_typed(
        self, running: tuple[BrokerClient, SimMailboxes]
    ) -> None:
        """CASE_EMAIL_NOT_RECEIVED raised in the broker is raised as EmailWaitError by the client."""
        client, mailboxes = running
        mailboxes.error = EmailWaitError("none", ReaderCaseKeyword.EMAIL_NOT_RECEIVED, details={"found": 0})
        with pytest.raises(EmailWaitError) as caught:
            client.wait_for_link("qa+other@example.com", Cta.ACTIVATE_ACCOUNT, timeout=5)
        assert caught.value.primary == ReaderCaseKeyword.EMAIL_NOT_RECEIVED and caught.value.details == {"found": 0}

    def EMI_TEST_BROKER_UNIT_130053_invalid_input_arrives_typed(
        self, running: tuple[BrokerClient, SimMailboxes]
    ) -> None:
        """Sender validation errors keep their class over the wire."""
        client, _mailboxes = running
        cyrillic = Marker.__new__(Marker)  # skip local validation so the broker's own check is exercised
        object.__setattr__(cyrillic, "test_id", "тест")
        object.__setattr__(cyrillic, "run_id", "")
        with pytest.raises(InvalidInputError) as caught:
            client.send("activation", _ADDRESS, marker=cyrillic)
        assert caught.value.primary == SenderCaseKeyword.CONDITION_INVALID_INPUT

    def EMI_TEST_BROKER_UNIT_130079_match_fields_over_http(self, running: tuple[BrokerClient, SimMailboxes]) -> None:
        """The client sends the recipient fields over HTTP and the reader receives them."""
        client, mailboxes = running
        client.wait_for_emails("qa+C400001_r@example.com", match_fields=("cc",), timeout=5.0)
        client.wait_for_link("qa+C400001_r@example.com", Cta.ACTIVATE_ACCOUNT, timeout=5.0)
        fields = [wait["match_fields"] for mailbox in mailboxes.built for wait in mailbox.waits]
        assert fields == [("cc",), None]

    def EMI_TEST_BROKER_UNIT_130054_post_needs_the_token(self, running: tuple[BrokerClient, SimMailboxes]) -> None:
        """A request with a wrong token is refused (401) even after re-reading the state file."""
        client, _mailboxes = running
        status, _body = client._http("POST", client.settings.base_url + "/lease",  # pylint: disable=protected-access
                                     b'{"run_id": "x"}', {"X-EMI-Broker-Token": "wrong"}, 5.0)
        assert status == 401

    @pytest.mark.network
    def EMI_TEST_BROKER_UNIT_130055_second_broker_cannot_bind(
        self, running: tuple[BrokerClient, SimMailboxes]
    ) -> None:
        """Binding is the lock: a second broker on the port fails with TEST_BROKER_PORT_UNAVAILABLE, logged."""
        client, _mailboxes = running
        settings = client.settings
        with pytest.raises(BrokerUnavailableError) as caught:
            bind(BrokerService(settings, sender=SimSender()))  # type: ignore[arg-type]
        assert caught.value.primary == TestBrokerKeyword.PORT_UNAVAILABLE
        log_text = log_path(settings.log_dir, date.today()).read_text(encoding="utf-8")
        assert f"TEST_BROKER_PORT_UNAVAILABLE port {settings.port} could not be opened" in log_text

    def EMI_TEST_BROKER_UNIT_130056_lease_keeper(self, running: tuple[BrokerClient, SimMailboxes]) -> None:
        """A run holds its lease for the block and releases it after."""
        client, _mailboxes = running
        with LeaseKeeper(client, "run-42", "pc pid 1"):
            assert "run-42" in (client.health() or {})["active_runs"]
        assert "run-42" not in (client.health() or {})["active_runs"]

    def EMI_TEST_BROKER_UNIT_130057_idle_shutdown(self, tmp_path: Path) -> None:
        """No requests and no leases for the idle limit: the broker stops and removes its state file."""
        clock = SimClock()
        settings = BrokerSettings(port=free_port(), log_dir=tmp_path / "logs", state_dir=tmp_path / "state",
                                  idle_shutdown_sec=1.0)
        server, state = bind(BrokerService(settings, sender=SimSender(), clock=clock.now))  # type: ignore[arg-type]
        thread = threading.Thread(target=serve, args=(server, state), daemon=True)
        thread.start()
        clock.advance(2.0)
        thread.join(timeout=5)
        assert not thread.is_alive() and not settings.state_file.exists()


class TestEMIBrokerAutoStart:
    """One start attempt, then a clear keyword."""

    def _client(self, answers: list[dict[str, object] | None], spawned: list[int],
                settings: BrokerSettings | None = None) -> BrokerClient:
        """Client whose /health answers come from a script."""
        def _http(method: str, url: str, body: bytes | None, headers: dict[str, str],
                  timeout: float) -> tuple[int, bytes]:
            del method, url, body, headers, timeout
            answer = answers.pop(0) if len(answers) > 1 else answers[0]
            if answer is None:
                raise ConnectionRefusedError(10061, "refused")
            return 200, json.dumps(answer).encode()

        clock = SimClock()
        return BrokerClient(settings or BrokerSettings(), http=_http,
                            spawner=lambda _settings: spawned.append(1), clock=clock.now, sleeper=clock.sleep)

    @pytest.mark.network
    def EMI_TEST_BROKER_UNIT_130060_starts_once_then_ready(self) -> None:
        """Nothing answers: one start, then the new broker answers."""
        settings = BrokerSettings()
        healthy = {"service": "emi_test_broker", "version": "1.0.0", "config_hash": settings.config_hash()}
        spawned: list[int] = []
        assert self._client([None, None, healthy], spawned, settings).ensure_running()["service"]
        assert spawned == [1]

    @pytest.mark.network
    def EMI_TEST_BROKER_UNIT_130061_still_down_is_unavailable(self) -> None:
        """Still nothing after one start: TEST_BROKER_UNAVAILABLE, no second start."""
        spawned: list[int] = []
        with pytest.raises(BrokerUnavailableError) as caught:
            self._client([None], spawned).ensure_running()
        assert caught.value.primary == TestBrokerKeyword.UNAVAILABLE and spawned == [1]

    def EMI_TEST_BROKER_UNIT_130062_other_version_is_mismatch(self) -> None:
        """Another version or config on the port: TEST_BROKER_VERSION_MISMATCH, nothing started."""
        spawned: list[int] = []
        old = {"service": "emi_test_broker", "version": "0.9.0", "config_hash": "abc", "pid": 77}
        with pytest.raises(BrokerUnavailableError) as caught:
            self._client([old], spawned).ensure_running()
        assert caught.value.primary == TestBrokerKeyword.VERSION_MISMATCH and not spawned
        assert caught.value.details["pid"] == 77

    @pytest.mark.network
    def EMI_TEST_BROKER_UNIT_130063_foreign_service_is_port_unavailable(self) -> None:
        """Something else answers on the port: TEST_BROKER_PORT_UNAVAILABLE."""
        with pytest.raises(BrokerUnavailableError) as caught:
            self._client([{"hello": "world"}], []).ensure_running()
        assert caught.value.primary == TestBrokerKeyword.PORT_UNAVAILABLE

    @pytest.mark.network
    def EMI_TEST_BROKER_UNIT_130064_auto_start_off(self) -> None:
        """With auto-start off nothing is started."""
        spawned: list[int] = []
        with pytest.raises(BrokerUnavailableError):
            self._client([None], spawned).ensure_running(auto_start=False)
        assert not spawned


class TestEMIBrokerLogAndCli:
    """Dated log with ISO 8601 offsets; CLI exit codes."""

    def EMI_TEST_BROKER_UNIT_130070_iso_timestamp_with_offset(self) -> None:
        """Local time with UTC offset and milliseconds."""
        record = logging.LogRecord("emi_test_broker", logging.INFO, __file__, 1, "x", None, None)
        stamp = IsoFormatter().formatTime(record)
        assert datetime.fromisoformat(stamp).utcoffset() is not None and "." in stamp

    def EMI_TEST_BROKER_UNIT_130071_fallback_file_when_main_cannot_open(self, tmp_path: Path) -> None:
        """The date's file cannot be opened: emi_test_broker_<date>_1.log is used."""
        day = date(2026, 10, 26)
        log_path(tmp_path, day).mkdir(parents=True)  # a directory where the file should be
        handler = open_log_handler(tmp_path, day)
        try:
            assert Path(handler.baseFilename).name == "emi_test_broker_2026-10-26_1.log"
        finally:
            handler.close()

    def EMI_TEST_BROKER_UNIT_130072_debug_entries_window_and_port_failures(self, tmp_path: Path) -> None:
        """Entries in the run's window, plus every port failure of that date."""
        now = datetime.now().astimezone()
        lines = [f"{(now - timedelta(hours=2)).isoformat()} ERROR TEST_BROKER_PORT_UNAVAILABLE port 8025 x",
                 f"{(now - timedelta(hours=1)).isoformat()} INFO old entry",
                 f"{now.isoformat()} INFO in window"]
        log_path(tmp_path, now.date()).write_text("\n".join(lines), encoding="utf-8")
        found = debug_entries(tmp_path, now - timedelta(minutes=5), now + timedelta(minutes=5))
        assert [line.split(" ", 2)[2] for line in found] == ["TEST_BROKER_PORT_UNAVAILABLE port 8025 x",
                                                             "in window"]

    @pytest.mark.network
    def EMI_TEST_BROKER_UNIT_130073_cli_status_without_broker_exits_3(self, tmp_path: Path) -> None:
        """No broker on the port: exit code 3."""
        assert main(["--port", str(free_port()), "--log-dir", str(tmp_path), "status"]) == 3

    def EMI_TEST_BROKER_UNIT_130074_cli_bad_age_exits_2(self, tmp_path: Path) -> None:
        """--older-than must be days; checked before any broker is started."""
        assert main(["--port", str(free_port()), "--log-dir", str(tmp_path), "purge", "--older-than", "1w"]) == 2
        assert not (tmp_path / START_ERRORS_LOG).exists()

    def EMI_TEST_BROKER_UNIT_130076_spawn_command_parses(self, tmp_path: Path) -> None:
        """The auto-start command is accepted by the broker's own parser (options before the subcommand)."""
        settings = BrokerSettings(port=9123, max_connections=4, log_dir=tmp_path, state_dir=tmp_path / "state")
        args = build_parser().parse_args(spawn_command(settings)[3:])
        assert (args.command, args.port, args.max_connections) == ("serve", 9123, 4)
        assert (args.log_dir, args.state_dir) == (tmp_path, tmp_path / "state")  # the token file is found
        assert args.settings_file == (tmp_path / "state" / "broker_9123_settings.json")

    def EMI_TEST_BROKER_UNIT_130077_settings_file_round_trip(self, tmp_path: Path) -> None:
        """The auto-start settings file rebuilds the caller's settings exactly (same config hash), no passwords."""
        imap = ImapSettings(host="mail.local", port=4143, folders=("INBOX", "Spam"), connect_timeout=7.0,
                            password="s3cret-imap")
        smtp = SenderSettings(host="mail.local", smtp_port=4025, base_mailbox="qa@corp.example", smtp_login=True,
                              password="s3cret-smtp")
        settings = BrokerSettings(port=9124, imap=imap, smtp=smtp, log_dir=Path("logs"), state_dir=tmp_path)
        data = settings_to_json(settings)
        assert "s3cret" not in json.dumps(data)
        rebuilt = settings_from_json(json.loads(json.dumps(data)),
                                     {"EMI_IMAP_PASSWORD": "s3cret-imap", "EMI_SMTP_PASSWORD": "s3cret-smtp"})
        assert rebuilt == settings and rebuilt.config_hash() == settings.config_hash()

    def EMI_TEST_BROKER_UNIT_130075_wait_queue_error_keeps_reader_keyword(self) -> None:
        """The broker's budget timeout travels as READER_IMAP_CONNECTION_LIMIT."""
        error = ImapConnectionError("busy", ReaderKeyword.IMAP_CONNECTION_LIMIT, infrastructure=True)
        assert error_from_json(error_to_json(error)).primary == ReaderKeyword.IMAP_CONNECTION_LIMIT  # type: ignore

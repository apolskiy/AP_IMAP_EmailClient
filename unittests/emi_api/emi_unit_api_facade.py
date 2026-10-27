# SPDX-FileCopyrightText: 2026 Aleksandr Polskiy
# SPDX-License-Identifier: Apache-2.0
"""Unit tests: ``EmiApi`` in both modes, health, leases and the family registry (design §8.2)."""

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from emi_api import (
    ClientMode,
    EmiApi,
    EmiSettings,
    LocalBroker,
    register_family,
    registered_families,
    unregister_family,
)
from emi_greenmail import GreenmailKeyword, GreenmailNotReadyError, Readiness
from emi_reader import Cta, EmailWaitError, ReaderCaseKeyword
from emi_test_broker import BrokerClient, BrokerService, BrokerSettings, TestBrokerKeyword
from unittests.emi_test_broker.broker_sims import LINK, SimMailboxes, SimSender, free_port, make_service, start_served

_RUN = "20261026t153012-w00-a1b2"


class SimGreenmailReadiness:
    """Greenmail client that is ready unless given an error."""

    def __init__(self, error: Exception | None = None) -> None:
        """Script the answer."""
        self.error = error
        self.checks = 0

    def check_ready(self) -> Readiness:
        """Raise the scripted error or report ready."""
        self.checks += 1
        if self.error is not None:
            raise self.error
        return Readiness(())


def _inprocess(tmp_path: Path, greenmail: SimGreenmailReadiness | None = None) -> tuple[
        EmiApi, SimMailboxes, SimSender]:
    """An in-process API over a service wired to simulations."""
    service, mailboxes, sender, _purge = make_service(tmp_path)
    settings = EmiSettings(mode=ClientMode.INPROCESS, broker=service.settings)
    api = EmiApi(settings, run_id=_RUN, backend=LocalBroker(service),
                 greenmail=greenmail or SimGreenmailReadiness())  # type: ignore[arg-type]
    return api, mailboxes, sender


@pytest.fixture(name="served")
def _served(tmp_path: Path) -> Iterator[tuple[BrokerSettings, BrokerService, SimMailboxes]]:
    """A real broker on a free loopback port, wired to simulations; stopped afterwards."""
    settings, service, mailboxes, thread = start_served(tmp_path)
    yield settings, service, mailboxes
    BrokerClient(settings, spawner=lambda _settings: None).stop()
    thread.join(timeout=5)


class TestEMIApiInprocess:
    """``inprocess`` mode runs the broker's request code without HTTP."""

    def EMI_API_UNIT_300040_send_sets_marker_from_address(self, tmp_path: Path) -> None:
        """The test address supplies the marker; the manifest comes back."""
        api, _mailboxes, sender = _inprocess(tmp_path)
        run = api.start_run("400001")
        manifest = api.sender.send("activation", address=run.address(), count=1)
        assert len(manifest) == 1 and manifest[0].link == LINK
        assert sender.batches[0][1] == run.marker

    def EMI_API_UNIT_300048_observers_see_manifests(self, tmp_path: Path) -> None:
        """Every send hands its manifest to the observers until they are removed."""
        api, _mailboxes, _sender = _inprocess(tmp_path)
        seen: list[tuple[str, Any]] = []
        remove = api.observe(lambda name, data: seen.append((name, data)))
        run = api.start_run("400001")
        api.sender.send("activation", address=run.address(), count=1)
        assert seen[0][0] == "manifest" and seen[0][1]["test_id"] == "C400001"
        assert seen[0][1]["entries"][0]["status"] == "sent"
        remove()
        remove()
        api.sender.send("activation", address=run.address(), count=1)
        assert len(seen) == 1

    def EMI_API_UNIT_300049_match_fields_passed_through(self, tmp_path: Path) -> None:
        """api.reader waits pass the recipient fields through to the reader (in-process mode)."""
        api, mailboxes, _sender = _inprocess(tmp_path)
        run = api.start_run("400001")
        api.reader.wait_for_link(run.address(), Cta.ACTIVATE_ACCOUNT, match_fields=("cc",))
        api.reader.wait_for_emails(run.address(), match_fields=("bcc", "delivered-to"))
        api.reader.wait_for_emails(run.address())
        fields = [wait["match_fields"] for wait in mailboxes.built[0].waits]
        assert fields == [("cc",), ("bcc", "delivered-to"), None]

    def EMI_API_UNIT_300062_settle_window_passed_through(self, tmp_path: Path) -> None:
        """api.reader.wait_for_emails passes the settle window through; None keeps the reader's default."""
        api, mailboxes, _sender = _inprocess(tmp_path)
        run = api.start_run("400001")
        api.reader.wait_for_emails(run.address(), count=2, settle_window=0.0)
        api.reader.wait_for_emails(run.address())
        assert [wait["settle_window"] for wait in mailboxes.built[0].waits] == [0.0, None]

    def EMI_API_UNIT_300063_observers_see_each_wait_inventory(self, tmp_path: Path) -> None:
        """After every wait the observers get its inventory: from the answer, or from a failed wait's error."""
        api, mailboxes, _sender = _inprocess(tmp_path)
        seen: list[tuple[str, Any]] = []
        api.observe(lambda name, data: seen.append((name, data)))
        address = api.start_run("400001").address()
        api.reader.wait_for_link(address, Cta.ACTIVATE_ACCOUNT)
        api.reader.wait_for_emails(address)
        mailboxes.built[0].error = EmailWaitError("none", ReaderCaseKeyword.EMAIL_NOT_RECEIVED,
                                                  details={"inventory": {"address": "from-error"}})
        with pytest.raises(EmailWaitError):
            api.reader.wait_for_link(address, Cta.ACTIVATE_ACCOUNT)
        assert [name for name, _data in seen] == ["inventory"] * 3
        assert seen[0][1] == mailboxes.built[0].inventory and seen[2][1] == {"address": "from-error"}

    def EMI_API_UNIT_300041_wait_for_link_default_timeout(self, tmp_path: Path) -> None:
        """Waits use the settings' wait timeout unless given one."""
        api, mailboxes, _sender = _inprocess(tmp_path)
        run = api.start_run("400001")
        assert api.reader.wait_for_link(run.address(), Cta.ACTIVATE_ACCOUNT) == LINK
        assert api.reader.wait_for_link(run.address(), Cta.ACTIVATE_ACCOUNT, timeout=5.0) == LINK
        timeouts = [wait["timeout"] for wait in mailboxes.built[0].waits]
        assert timeouts[0] == pytest.approx(60.0) and timeouts[1] == pytest.approx(5.0)

    def EMI_API_UNIT_300042_wait_for_emails(self, tmp_path: Path) -> None:
        """Emails come back typed, oldest first."""
        api, _mailboxes, _sender = _inprocess(tmp_path)
        emails = api.reader.wait_for_emails(api.start_run("400001").address(), count=2)
        assert [item.uid for item in emails] == [1, 2]

    def EMI_API_UNIT_300043_typed_error_unchanged(self, tmp_path: Path) -> None:
        """A reader error reaches the test as the same class with its keywords."""
        api, mailboxes, _sender = _inprocess(tmp_path)
        mailboxes.error = EmailWaitError("none", ReaderCaseKeyword.EMAIL_NOT_RECEIVED)
        with pytest.raises(EmailWaitError) as caught:
            api.reader.wait_for_link(api.start_run("400001").address(), Cta.ACTIVATE_ACCOUNT)
        assert caught.value.primary == ReaderCaseKeyword.EMAIL_NOT_RECEIVED

    def EMI_API_UNIT_300044_health_ready(self, tmp_path: Path) -> None:
        """Ready: no problems, the service's health and redacted settings."""
        api, _mailboxes, _sender = _inprocess(tmp_path)
        health = api.health()
        assert health.ready and health.mode == "inprocess" and health.keywords == ("", ())
        assert health.broker is not None and health.broker["service"] == "emi_test_broker"

    @pytest.mark.network
    def EMI_API_UNIT_300045_health_greenmail_down(self, tmp_path: Path) -> None:
        """Greenmail not ready: health says why with the keywords and never raises."""
        error = GreenmailNotReadyError("imap port 3143 closed", GreenmailKeyword.IMAP_CONNECTION_FAILURE,
                                       secondary=(GreenmailKeyword.NOT_READY,), infrastructure=True)
        api, _mailboxes, _sender = _inprocess(tmp_path, SimGreenmailReadiness(error))
        health = api.health()
        assert not health.ready and health.problems[0].component == "greenmail"
        assert health.keywords == ("GREENMAIL_IMAP_CONNECTION_FAILURE", ("GREENMAIL_NOT_READY",))

    def EMI_API_UNIT_300046_close_closes_pool(self, tmp_path: Path) -> None:
        """Leaving the block closes pooled connections."""
        api, mailboxes, _sender = _inprocess(tmp_path)
        with api:
            api.reader.wait_for_link(api.start_run("400001").address(), Cta.ACTIVATE_ACCOUNT)
        assert mailboxes.built[0].closed == 1

    def EMI_API_UNIT_300047_run_id_from_environment(self, tmp_path: Path) -> None:
        """EMI_RUN_ID is shared by the run; each worker gets its own number."""
        service, _mailboxes, _sender, _purge = make_service(tmp_path)
        api = EmiApi(EmiSettings(mode=ClientMode.INPROCESS, broker=service.settings),
                     environ={"EMI_RUN_ID": _RUN, "PYTEST_XDIST_WORKER": "gw4"}, backend=LocalBroker(service),
                     greenmail=SimGreenmailReadiness())  # type: ignore[arg-type]
        assert api.run_id == "20261026t153012-w04-a1b2"


class TestEMIApiService:
    """``service`` mode goes through a real loopback broker."""

    def EMI_API_UNIT_300050_start_run_registers_lease(
            self, served: tuple[BrokerSettings, BrokerService, SimMailboxes]) -> None:
        """The first start_run leases the run once; close releases it."""
        settings, service, _mailboxes = served
        client = BrokerClient(settings, spawner=lambda _settings: None)
        api = EmiApi(EmiSettings(broker=settings), run_id=_RUN, backend=client,
                     greenmail=SimGreenmailReadiness())  # type: ignore[arg-type]
        api.start_run("400001")
        api.start_run("400002")
        assert service.health()["active_runs"] == [_RUN]
        api.close()
        assert service.health()["active_runs"] == []

    def EMI_API_UNIT_300051_send_and_wait_over_http(
            self, served: tuple[BrokerSettings, BrokerService, SimMailboxes]) -> None:
        """Send and wait round-trip through the broker; the wait's inventory reaches the observers."""
        settings, _service, _mailboxes = served
        client = BrokerClient(settings, spawner=lambda _settings: None)
        with EmiApi(EmiSettings(broker=settings), run_id=_RUN, backend=client,
                    greenmail=SimGreenmailReadiness()) as api:  # type: ignore[arg-type]
            run = api.start_run("400001")
            assert api.sender.send("activation", address=run.address())[0].link == LINK
            seen: list[tuple[str, Any]] = []
            api.observe(lambda name, data: seen.append((name, data)))
            assert api.reader.wait_for_link(run.address(), Cta.ACTIVATE_ACCOUNT, timeout=5.0) == LINK
            assert seen == [("inventory", client.last_inventory)] and seen[0][1]["address"]  # crossed HTTP
            assert api.health().ready

    def EMI_API_UNIT_300052_typed_error_over_http(
            self, served: tuple[BrokerSettings, BrokerService, SimMailboxes]) -> None:
        """The same error class reaches the test in service mode."""
        settings, _service, mailboxes = served
        mailboxes.error = EmailWaitError("none", ReaderCaseKeyword.EMAIL_NOT_RECEIVED)
        client = BrokerClient(settings, spawner=lambda _settings: None)
        with EmiApi(EmiSettings(broker=settings), run_id=_RUN, backend=client,
                    greenmail=SimGreenmailReadiness()) as api:  # type: ignore[arg-type]
            with pytest.raises(EmailWaitError) as caught:
                api.reader.wait_for_link(api.start_run("400001").address(), Cta.ACTIVATE_ACCOUNT, timeout=5.0)
        assert caught.value.primary == ReaderCaseKeyword.EMAIL_NOT_RECEIVED

    @pytest.mark.network
    def EMI_API_UNIT_300053_health_broker_down(self, tmp_path: Path) -> None:
        """No broker and no auto-start: health reports TEST_BROKER_UNAVAILABLE without raising."""
        settings = BrokerSettings(port=free_port(), log_dir=tmp_path / "logs", state_dir=tmp_path / "state")
        client = BrokerClient(settings, spawner=lambda _settings: None)
        api = EmiApi(EmiSettings(broker=settings, auto_start=False), run_id=_RUN, backend=client,
                     greenmail=SimGreenmailReadiness())  # type: ignore[arg-type]
        health = api.health()
        assert not health.ready and health.keywords[0] == TestBrokerKeyword.UNAVAILABLE
        assert health.problems[0].component == "broker"

    def EMI_API_UNIT_300054_wrapped_client_keeps_service_mode(
            self, served: tuple[BrokerSettings, BrokerService, SimMailboxes]) -> None:
        """A wrapper exposing ``inner`` (a named fault) still gets the broker's health and run lease."""
        settings, service, _mailboxes = served
        wrapper = SimWrapper(BrokerClient(settings, spawner=lambda _settings: None))
        api = EmiApi(EmiSettings(broker=settings), run_id=_RUN, backend=wrapper,  # type: ignore[arg-type]
                     greenmail=SimGreenmailReadiness())  # type: ignore[arg-type]
        assert api.health().ready
        api.start_run("400001")
        assert service.health()["active_runs"] == [_RUN]
        api.close()
        assert service.health()["active_runs"] == []


class SimWrapper:
    """A backend wrapper as the harness's named faults build it: ``inner`` plus pass-through."""

    def __init__(self, inner: BrokerClient) -> None:
        """Wrap a client."""
        self._inner = inner

    @property
    def inner(self) -> BrokerClient:
        """The wrapped client."""
        return self._inner

    def __getattr__(self, name: str) -> Any:
        """Every call reaches the wrapped client."""
        return getattr(self._inner, name)


class TestEMIApiFamilies:
    """New test families plug in without core changes."""

    def EMI_API_UNIT_300060_family_built_once(self, tmp_path: Path) -> None:
        """A registered family's helper is built on first use and reused."""
        built: list[Any] = []

        @register_family("unit-300060")
        def _factory(api: EmiApi) -> dict[str, Any]:
            built.append(api)
            return {"api": api}

        try:
            api, _mailboxes, _sender = _inprocess(tmp_path)
            assert "unit-300060" in registered_families()
            assert api.family("unit-300060") is api.family("unit-300060") and len(built) == 1
        finally:
            unregister_family("unit-300060")

    def EMI_API_UNIT_300061_family_errors(self, tmp_path: Path) -> None:
        """Duplicate registration and unknown names are refused."""
        register_family("unit-300061")(lambda api: api)
        try:
            with pytest.raises(ValueError):
                register_family("unit-300061")(lambda api: api)
            api, _mailboxes, _sender = _inprocess(tmp_path)
            with pytest.raises(KeyError):
                api.family("no-such-family")
        finally:
            unregister_family("unit-300061")

# SPDX-FileCopyrightText: 2026 Aleksandr Polskiy
# SPDX-License-Identifier: Apache-2.0
"""Unit tests: broker request handling, leases, connection budget and purge (design §7)."""

import threading
from datetime import date
from pathlib import Path

import pytest

from emi_reader import Cta, EmailWaitError, ImapConnectionError, ImapSettings, ReaderCaseKeyword, ReaderKeyword
from emi_sender import InvalidInputError
from emi_test_broker import BrokerSettings, PurgeRefusedError, TestBrokerKeyword
from emi_test_broker.codec import cta_to_json
from emi_test_broker.leases import ConnectionBudget, LeaseTable
from emi_test_broker.purge import PurgeRequest, search_criteria
from unittests.emi_test_broker.broker_sims import LINK, make_service
from unittests.sims import SimClock

_ADDRESS = "qa+400001_run-1@example.com"
_WAIT = {"address": _ADDRESS, "cta": cta_to_json(Cta.ACTIVATE_ACCOUNT), "timeout": 5, "run_id": "run-1"}


class TestEMIBrokerHealthAndSettings:
    """Identity and settings in effect, never a password."""

    def EMI_TEST_BROKER_UNIT_130001_health_identity_and_redaction(self, tmp_path: Path) -> None:
        """Service name, version, config hash, pool; the password is redacted."""
        service, *_sims = make_service(tmp_path, imap=ImapSettings(password="Sup3r-secret"))
        health = service.health()
        assert health["service"] == "emi_test_broker" and health["ready"]
        assert health["pool"] == {"used": 0, "limit": 10}
        assert "Sup3r-secret" not in str(health) and health["settings"]["imap"]["password"] == "***"

    def EMI_TEST_BROKER_UNIT_130002_config_hash_tracks_settings_and_password(self) -> None:
        """Same settings, same hash; another password or budget, another hash."""
        base = BrokerSettings()
        assert base.config_hash() == BrokerSettings().config_hash()
        assert base.config_hash() != BrokerSettings(imap=ImapSettings(password="other")).config_hash()
        assert base.config_hash() != BrokerSettings(max_connections=3).config_hash()
        assert "password" not in base.config_hash()

    @pytest.mark.parametrize("bad_kwargs", [{"port": 0}, {"max_connections": 0}, {"lease_ttl_sec": 0.5}])
    def EMI_TEST_BROKER_UNIT_130003_settings_ranges(self, bad_kwargs: dict[str, object]) -> None:
        """Port, budget and time limits are validated."""
        with pytest.raises(ValueError):
            BrokerSettings(**bad_kwargs)  # type: ignore[arg-type]


class TestEMIBrokerSendAndWait:
    """The broker forwards to the same library code."""

    def EMI_TEST_BROKER_UNIT_130010_send_forwards_kind_address_and_marker(self, tmp_path: Path) -> None:
        """One kind to one address, marker kept, manifest returned."""
        service, _mailboxes, sender, _purge = make_service(tmp_path)
        answer = service.send({"kind": "activation", "address": _ADDRESS, "count": 1,
                               "marker": {"test_id": "400001", "run_id": "run-1"}})
        request, marker = sender.batches[0]
        assert request.addresses == (_ADDRESS,) and dict(request.counts) == {"activation": 1}
        assert marker is not None and marker.run_id == "run-1"
        assert answer["entries"][0]["status"] == "sent" and answer["run_id"] == "run-1"

    def EMI_TEST_BROKER_UNIT_130011_send_rejects_bad_marker(self, tmp_path: Path) -> None:
        """A Cyrillic test ID is invalid input, as in-process."""
        service, *_sims = make_service(tmp_path)
        with pytest.raises(InvalidInputError):
            service.send({"kind": "activation", "address": _ADDRESS, "marker": {"test_id": "тест"}})

    def EMI_TEST_BROKER_UNIT_130012_wait_returns_link_and_matrix(self, tmp_path: Path) -> None:
        """Link mode: the link plus the accessibility matrix for the report."""
        service, mailboxes, *_sims = make_service(tmp_path)
        answer = service.wait(dict(_WAIT))
        assert answer["link"] == LINK and answer["access_matrix"][0]["access"] == "accessible"
        assert mailboxes.built[0].waits[0]["cta"] == Cta.ACTIVATE_ACCOUNT

    def EMI_TEST_BROKER_UNIT_130013_wait_emails_mode(self, tmp_path: Path) -> None:
        """Emails mode returns every email with its decoded parts."""
        service, *_sims = make_service(tmp_path)
        answer = service.wait({**_WAIT, "mode": "emails", "count": 2})
        assert [item["uid"] for item in answer["emails"]] == [1, 2]
        assert answer["emails"][0]["email"]["recipients"]["to"] == [_ADDRESS]

    def EMI_TEST_BROKER_UNIT_130014_connection_reused_between_waits(self, tmp_path: Path) -> None:
        """A pooled mailbox (open IMAP connection) serves the next wait."""
        service, mailboxes, *_sims = make_service(tmp_path)
        service.wait(dict(_WAIT))
        service.wait(dict(_WAIT))
        assert len(mailboxes.built) == 1 and len(mailboxes.built[0].waits) == 2

    def EMI_TEST_BROKER_UNIT_130015_failed_wait_closes_connection(self, tmp_path: Path) -> None:
        """A failed wait raises the reader's error and closes the connection so the next one reconnects."""
        service, mailboxes, *_sims = make_service(tmp_path)
        mailboxes.error = EmailWaitError("none", ReaderCaseKeyword.EMAIL_NOT_RECEIVED)
        with pytest.raises(EmailWaitError):
            service.wait(dict(_WAIT))
        assert mailboxes.built[0].closed == 1

    def EMI_TEST_BROKER_UNIT_130016_link_mode_needs_cta(self, tmp_path: Path) -> None:
        """Without a CTA there is no link to return."""
        service, *_sims = make_service(tmp_path)
        with pytest.raises(ValueError):
            service.wait({"address": _ADDRESS, "timeout": 1})

    def EMI_TEST_BROKER_UNIT_130017_budget_exhausted_is_connection_limit(self, tmp_path: Path) -> None:
        """All connections busy for the whole wait: READER_IMAP_CONNECTION_LIMIT, environment failure."""
        service, mailboxes, *_sims = make_service(tmp_path, max_connections=1)
        caught: list[Exception] = []

        def _second_wait() -> None:
            try:
                service.wait({**_WAIT, "timeout": 0.05})
            except ImapConnectionError as error:
                caught.append(error)

        def _second_wait_while_first_holds_the_slot() -> None:
            thread = threading.Thread(target=_second_wait)
            thread.start()
            thread.join()

        mailboxes.on_wait = _second_wait_while_first_holds_the_slot
        service.wait(dict(_WAIT))
        assert caught and caught[0].primary == ReaderKeyword.IMAP_CONNECTION_LIMIT  # type: ignore[attr-defined]
        assert caught[0].infrastructure  # type: ignore[attr-defined]


class TestEMIBrokerLeases:
    """Run leases expire without heartbeats; waits count as active runs."""

    def EMI_TEST_BROKER_UNIT_130020_lease_lifecycle(self) -> None:
        """Acquire, heartbeat, expire after the TTL, release."""
        clock = SimClock()
        table = LeaseTable(ttl_sec=300, clock=clock.now)
        table.acquire("run-1", "host pid 1")
        clock.advance(200)
        assert table.heartbeat("run-1")
        clock.advance(299)
        assert table.active() == {"run-1": "host pid 1"}
        clock.advance(2)
        assert not table.active() and not table.heartbeat("run-1")
        table.acquire("run-2", "host")
        table.release("run-2")
        assert not table.active()

    def EMI_TEST_BROKER_UNIT_130021_wait_counts_as_active(self) -> None:
        """A wait in progress is an active run even without a lease."""
        table = LeaseTable(ttl_sec=300, clock=SimClock().now)
        with table.waiting("run-9"):
            assert table.active() == {"run-9": "wait in progress"}
        assert not table.active()

    def EMI_TEST_BROKER_UNIT_130022_lease_endpoint_actions(self, tmp_path: Path) -> None:
        """acquire / heartbeat / release through the service; unknown action rejected."""
        service, *_sims = make_service(tmp_path)
        assert service.lease("acquire", {"run_id": "run-1", "holder": "pc"})["ttl_sec"] == 300
        assert service.lease("heartbeat", {"run_id": "run-1"})["renewed"]
        assert service.lease("release", {"run_id": "run-1"})["released"]
        with pytest.raises(ValueError):
            service.lease("steal", {"run_id": "run-1"})

    def EMI_TEST_BROKER_UNIT_130023_budget_caps_and_queues(self) -> None:
        """Slots are capped at the limit; a full budget times out."""
        budget = ConnectionBudget(2)
        with budget.slots(5) as granted:
            assert granted == 2 and budget.used == 2
            with pytest.raises(TimeoutError):
                with budget.slots(1, timeout=0.01):
                    pass
        assert budget.used == 0


class TestEMIBrokerPurge:
    """Manual purge: refused while runs are active, marker-only by default."""

    def EMI_TEST_BROKER_UNIT_130030_refused_with_active_lease(self, tmp_path: Path) -> None:
        """TEST_BROKER_PURGE_REFUSED names the active run IDs; nothing is touched."""
        service, _mailboxes, _sender, purge = make_service(tmp_path)
        service.lease("acquire", {"run_id": "run-7", "holder": "pc"})
        with pytest.raises(PurgeRefusedError) as caught:
            service.purge({})
        assert caught.value.primary == TestBrokerKeyword.PURGE_REFUSED
        assert caught.value.details["active_runs"] == ["run-7"] and "run-7" in str(caught.value)
        assert not purge.criteria

    def EMI_TEST_BROKER_UNIT_130031_refused_during_a_wait(self, tmp_path: Path) -> None:
        """A wait in progress blocks a purge too."""
        service, mailboxes, *_sims = make_service(tmp_path)
        refused: list[PurgeRefusedError] = []

        def _try_purge() -> None:
            try:
                service.purge({})
            except PurgeRefusedError as error:
                refused.append(error)

        mailboxes.on_wait = _try_purge
        service.wait(dict(_WAIT))
        assert refused and refused[0].details["active_runs"] == ["run-1"]

    def EMI_TEST_BROKER_UNIT_130032_dry_run_counts_only(self, tmp_path: Path) -> None:
        """Dry run: counts per folder, nothing flagged or expunged."""
        service, _mailboxes, _sender, purge = make_service(tmp_path)
        report = service.purge({"dry_run": True, "requested_by": "tester"})
        assert report == {"dry_run": True, "scope": 'HEADER X-EMI-Test-Id ""', "counts": {"INBOX": 2},
                          "total": 2, "requested_by": "tester"}
        assert not purge.deleted and purge.expunged == 0 and purge.closed

    def EMI_TEST_BROKER_UNIT_130033_purge_flags_and_expunges(self, tmp_path: Path) -> None:
        """A real purge flags \\Deleted and expunges."""
        service, _mailboxes, _sender, purge = make_service(tmp_path)
        assert service.purge({})["total"] == 2
        assert purge.deleted == [3, 4] and purge.expunged == 1

    def EMI_TEST_BROKER_UNIT_130034_scope_criteria(self) -> None:
        """Marker-only by default, everything with --all, IMAP BEFORE date for --older-than."""
        today = date(2026, 10, 26)
        assert search_criteria(PurgeRequest(), today) == 'HEADER X-EMI-Test-Id ""'
        assert search_criteria(PurgeRequest(everything=True, older_than_days=7), today) == "ALL BEFORE 19-Oct-2026"

    def EMI_TEST_BROKER_UNIT_130035_unreachable_server_has_reader_keyword(self, tmp_path: Path) -> None:
        """The purge connection failing is classified like the reader's."""
        service, *_sims = make_service(tmp_path)

        def _refuse(settings: ImapSettings) -> None:
            del settings
            raise ConnectionRefusedError(10061, "refused")

        service._purge_factory = _refuse  # pylint: disable=protected-access
        with pytest.raises(ImapConnectionError) as caught:
            service.purge({})
        assert caught.value.primary == ReaderKeyword.IMAP_CONNECTION_REFUSED

    def EMI_TEST_BROKER_UNIT_130036_negative_age_rejected(self, tmp_path: Path) -> None:
        """--older-than must not be negative."""
        service, *_sims = make_service(tmp_path)
        with pytest.raises(ValueError):
            service.purge({"older_than_days": -1})

# SPDX-FileCopyrightText: 2026 Aleksandr Polskiy
# SPDX-License-Identifier: Apache-2.0
"""Unit tests: Greenmail readiness, port probes and management calls (design §8.1)."""

import json
import logging

import pytest

from emi_greenmail import (
    GreenmailApiError,
    GreenmailClient,
    GreenmailKeyword,
    GreenmailNotReadyError,
    GreenmailSettings,
)
from unittests.emi_greenmail.greenmail_sims import SimGreenmail
from unittests.sims import SimClock


class TestEMIGreenmailReadiness:
    """Ready only when the REST check passes and every mail port accepts connections."""

    def EMI_GREENMAIL_UNIT_120001_ready_when_api_and_ports_answer(self) -> None:
        """REST readiness plus SMTP and IMAP probes all pass."""
        result = SimGreenmail().client().check_ready()
        assert result.ready
        assert [probe.name for probe in result.probes] == ["api", "smtp", "imap"]

    @pytest.mark.network
    def EMI_GREENMAIL_UNIT_120002_api_down_is_not_ready(self) -> None:
        """Nothing on 8080: GREENMAIL_NOT_READY naming the host:port probed, as an environment failure."""
        server = SimGreenmail()
        server.api_down = True
        with pytest.raises(GreenmailNotReadyError) as caught:
            server.client().check_ready()
        assert caught.value.primary == GreenmailKeyword.NOT_READY
        assert caught.value.infrastructure
        assert "api 127.0.0.1:8080" in str(caught.value) and "README" in str(caught.value)

    @pytest.mark.network
    def EMI_GREENMAIL_UNIT_120003_closed_mail_port_is_not_ready(self) -> None:
        """API answers but IMAP is not published: IMAP keyword leads, only the failed endpoint is named."""
        server = SimGreenmail()
        server.closed_ports.add(3143)
        with pytest.raises(GreenmailNotReadyError) as caught:
            server.client().check_ready()
        assert [failed["endpoint"] for failed in caught.value.details["failed"]] == ["127.0.0.1:3143"]
        assert caught.value.primary == GreenmailKeyword.IMAP_CONNECTION_FAILURE
        assert caught.value.secondary == (GreenmailKeyword.NOT_READY,)

    @pytest.mark.network
    def EMI_GREENMAIL_UNIT_120008_closed_smtp_port_has_smtp_keyword(self) -> None:
        """SMTP closed: GREENMAIL_SMTP_CONNECTION_FAILURE leads."""
        server = SimGreenmail()
        server.closed_ports.add(3025)
        with pytest.raises(GreenmailNotReadyError) as caught:
            server.client().check_ready()
        assert caught.value.primary == GreenmailKeyword.SMTP_CONNECTION_FAILURE

    @pytest.mark.network
    def EMI_GREENMAIL_UNIT_120009_both_ports_closed(self) -> None:
        """SMTP and IMAP closed, API up: the first failed protocol leads, the other is secondary."""
        server = SimGreenmail()
        server.closed_ports.update({3025, 3143})
        with pytest.raises(GreenmailNotReadyError) as caught:
            server.client().check_ready()
        assert caught.value.primary == GreenmailKeyword.SMTP_CONNECTION_FAILURE
        assert caught.value.secondary == (GreenmailKeyword.IMAP_CONNECTION_FAILURE, GreenmailKeyword.NOT_READY)

    @pytest.mark.network
    def EMI_GREENMAIL_UNIT_120010_server_down_keeps_not_ready_primary(self) -> None:
        """API and ports down: the server is down, GREENMAIL_NOT_READY leads, ports are context."""
        server = SimGreenmail()
        server.api_down = True
        server.closed_ports.update({3025, 3143})
        with pytest.raises(GreenmailNotReadyError) as caught:
            server.client().check_ready()
        assert caught.value.primary == GreenmailKeyword.NOT_READY
        assert caught.value.secondary == (GreenmailKeyword.SMTP_CONNECTION_FAILURE,
                                          GreenmailKeyword.IMAP_CONNECTION_FAILURE)

    def EMI_GREENMAIL_UNIT_120011_tls_ports_map_to_their_protocol(self) -> None:
        """A closed IMAPS port is an IMAP connection failure."""
        server = SimGreenmail()
        server.closed_ports.add(3993)
        client = GreenmailClient(GreenmailSettings(probe_ports={"smtp": 3025, "imaps": 3993}), http=server.http,
                                 connector=server.connect)
        with pytest.raises(GreenmailNotReadyError) as caught:
            client.check_ready()
        assert caught.value.primary == GreenmailKeyword.IMAP_CONNECTION_FAILURE

    def EMI_GREENMAIL_UNIT_120004_not_ready_answer_is_not_ready(self) -> None:
        """HTTP 503 from the readiness endpoint (server still starting)."""
        server = SimGreenmail()
        server.ready_after_calls = 1
        client = server.client()
        assert not client.ready()
        assert client.ready()

    def EMI_GREENMAIL_UNIT_120005_ready_never_raises(self) -> None:
        """``ready()`` reports False instead of raising."""
        server = SimGreenmail()
        server.api_down = True
        assert server.client().ready() is False

    def EMI_GREENMAIL_UNIT_120006_wait_until_ready_polls(self) -> None:
        """Polls every 0.5 s until ready."""
        server = SimGreenmail()
        server.ready_after_calls = 3
        clock = SimClock()
        assert server.client(clock).wait_until_ready(timeout=10).ready
        assert clock.sleeps == [0.5, 0.5, 0.5]

    def EMI_GREENMAIL_UNIT_120007_wait_until_ready_gives_up(self) -> None:
        """Never ready: GREENMAIL_NOT_READY after the timeout, naming the wait."""
        server = SimGreenmail()
        server.api_down = True
        clock = SimClock()
        with pytest.raises(GreenmailNotReadyError) as caught:
            server.client(clock).wait_until_ready(timeout=2)
        assert "after waiting 2 s" in str(caught.value)
        assert clock.current <= 2.0


class TestEMIGreenmailManagement:
    """Configuration, users and purge."""

    def EMI_GREENMAIL_UNIT_120020_users_listed(self) -> None:
        """The user list is read from /api/user."""
        users = SimGreenmail().client().users()
        assert [(user.login, user.email) for user in users] == [("qa", "qa@example.com")]

    def EMI_GREENMAIL_UNIT_120021_configuration_read(self) -> None:
        """The configuration is returned as reported."""
        assert SimGreenmail().client().configuration()["serverSetups"][0]["port"] == 3025

    def EMI_GREENMAIL_UNIT_120022_create_user_sends_body_and_hides_password(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """POST /api/user with email, login and password; the password is never logged."""
        server = SimGreenmail()
        with caplog.at_level(logging.INFO):
            user = server.client().create_user("qa2@example.com", "qa2", "Sup3r-secret")
        method, url, body, _timeout = server.calls[-1]
        assert (method, url) == ("POST", "http://127.0.0.1:8080/api/user")
        assert json.loads(body or b"") == {"email": "qa2@example.com", "login": "qa2", "password": "Sup3r-secret"}
        assert user.login == "qa2"
        assert "Sup3r-secret" not in caplog.text

    def EMI_GREENMAIL_UNIT_120023_delete_user_quotes_the_address(self) -> None:
        """The plus sign and @ are URL-encoded."""
        server = SimGreenmail()
        server.client().delete_user("qa+1@example.com")
        assert server.calls[-1][:2] == ("DELETE", "http://127.0.0.1:8080/api/user/qa%2B1%40example.com")

    def EMI_GREENMAIL_UNIT_120024_purge_posts(self) -> None:
        """POST /api/mail/purge."""
        server = SimGreenmail()
        server.client().purge()
        assert server.calls[-1][:2] == ("POST", "http://127.0.0.1:8080/api/mail/purge")

    def EMI_GREENMAIL_UNIT_120025_api_error_has_its_keyword(self) -> None:
        """An error answer raises GREENMAIL_API_REQUEST_FAILED with status and reason."""
        server = SimGreenmail()
        # Greenmail 2.1.15 answers 400 (not the 404 its OpenAPI spec lists) for an unknown user (Spike Evidence)
        server.routes[("DELETE", "/api/user/nobody")] = (400, b'{"message":"User not found"}')
        with pytest.raises(GreenmailApiError) as caught:
            server.client().delete_user("nobody")
        assert caught.value.primary == GreenmailKeyword.API_REQUEST_FAILED
        assert caught.value.details["status"] == 400 and "User not found" in str(caught.value)
        assert not caught.value.infrastructure

    @pytest.mark.network
    def EMI_GREENMAIL_UNIT_120026_unreachable_api_is_not_ready(self) -> None:
        """A management call to a stopped server raises GREENMAIL_NOT_READY, not an API error."""
        server = SimGreenmail()
        server.api_down = True
        with pytest.raises(GreenmailNotReadyError):
            server.client().users()

    def EMI_GREENMAIL_UNIT_120027_unexpected_payload_is_api_error(self) -> None:
        """A user list that is not a JSON array is reported, not silently ignored."""
        server = SimGreenmail()
        server.routes[("GET", "/api/user")] = (200, b"<html>proxy</html>")
        with pytest.raises(GreenmailApiError):
            server.client().users()


class TestEMIGreenmailSettings:
    """Settings validation."""

    @pytest.mark.parametrize("bad_kwargs", [{"api_port": 0}, {"probe_ports": {"imap": 70000}}, {"timeout": 0}])
    def EMI_GREENMAIL_UNIT_120040_out_of_range_rejected(self, bad_kwargs: dict[str, object]) -> None:
        """Ports 1-65535, timeout 0.1-300 s."""
        with pytest.raises(ValueError):
            GreenmailSettings(**bad_kwargs)  # type: ignore[arg-type]

    def EMI_GREENMAIL_UNIT_120041_custom_probe_ports(self) -> None:
        """IMAPS can be probed too (TLS test runs)."""
        settings = GreenmailSettings(probe_ports={"smtp": 3025, "imap": 3143, "imaps": 3993})
        assert settings.describe()["probe_ports"]["imaps"] == 3993 and settings.base_url.endswith(":8080")

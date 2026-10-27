# SPDX-FileCopyrightText: 2026 Aleksandr Polskiy
# SPDX-License-Identifier: Apache-2.0
"""Unit tests: the real ``smtplib`` sender through a fault proxy to a simulated SMTP server (design §13.2).

Layer 2 of the network faults (AMB-77): real sockets, real ``smtplib``, faults injected on the wire.
"""

from collections.abc import Iterator

import pytest

from emi_sender import Sender, SenderKeyword, SenderSettings
from unittests.netfaults.fault_proxy import BLACKHOLE, CLOSE_ON_CLIENT_WRITE, REFUSE, RESET_AFTER_BYTES, FaultProxy
from unittests.netfaults.sim_smtp_server import SimSmtpServer

_ADDRESS = "qa+400200_20261026t153012-w01-a1b2@example.com"
# Windows retries a refused connect for about 2 s before failing, so a shorter connect timeout would
# report a refused port as a connect timeout.
_REFUSE_CONNECT_SEC = 5.0


@pytest.fixture(name="smtp")
def _smtp() -> Iterator[tuple[SimSmtpServer, FaultProxy]]:
    """A simulated SMTP server behind a fault proxy."""
    with SimSmtpServer() as server, FaultProxy(server.port) as proxy:
        yield server, proxy


def _sender(proxy: FaultProxy, connect_timeout: float = 1.0) -> Sender:
    """A sender that connects through the proxy with short timeouts."""
    return Sender(SenderSettings(smtp_port=proxy.port, connect_timeout=connect_timeout, read_timeout=1.0))


@pytest.mark.network
class TestEMISenderFaultProxy:
    """Each fault mode against the real client."""

    def EMI_SENDER_UNIT_110066_refused_port_exits_3_without_retry(
            self, smtp: tuple[SimSmtpServer, FaultProxy]) -> None:
        """Nothing listening: SENDER_SMTP_CONNECTION_REFUSED for every email, exit code 3, nothing sent."""
        server, proxy = smtp
        proxy.set_mode(REFUSE)
        manifest = _sender(proxy, _REFUSE_CONNECT_SEC).send("activation", _ADDRESS, 2)
        assert manifest.exit_code == 3
        assert [entry.keyword for entry in manifest.entries] == [SenderKeyword.SMTP_CONNECTION_REFUSED] * 2
        assert not server.messages

    def EMI_SENDER_UNIT_110067_silent_server_is_a_connect_timeout(
            self, smtp: tuple[SimSmtpServer, FaultProxy]) -> None:
        """A server that accepts and never greets: SENDER_SMTP_CONNECTION_TIMEOUT, exit code 3."""
        server, proxy = smtp
        proxy.set_mode(BLACKHOLE)
        manifest = _sender(proxy).send("activation", _ADDRESS)
        assert manifest.exit_code == 3 and manifest.entries[0].keyword == SenderKeyword.SMTP_CONNECTION_TIMEOUT
        assert not server.messages

    @pytest.mark.parametrize(("mode", "options"), [
        (RESET_AFTER_BYTES, {"after_bytes": 60}),
        (CLOSE_ON_CLIENT_WRITE, {"after_writes": 3}),
    ], ids=["reset-mid-session", "closed-on-write"])
    def EMI_SENDER_UNIT_110068_mid_session_fault_is_retried(
            self, smtp: tuple[SimSmtpServer, FaultProxy], mode: str, options: dict[str, int]) -> None:
        """A connection broken mid-session is retried on a new one; the email arrives once."""
        server, proxy = smtp
        proxy.set_mode(mode, once=True, **options)
        manifest = _sender(proxy).send("activation", _ADDRESS)
        assert manifest.exit_code == 0 and manifest.entries[0].attempts == 2 and proxy.faults == 1
        assert len(server.messages) == 1 and _ADDRESS.encode("ascii") in server.messages[0]

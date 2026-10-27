# SPDX-FileCopyrightText: 2026 Aleksandr Polskiy
# SPDX-License-Identifier: Apache-2.0
"""Unit tests: the real ``imaplib`` reader through a fault proxy to a simulated IMAP server (design §13.2).

Layer 2 of the network faults (AMB-77): real sockets, real ``imaplib``, faults injected on the wire, so the
keywords and retry decisions are checked against what the operating system actually raises.
"""

import time
from collections.abc import Iterator

import pytest

from emi_reader import Cta, ImapConnectionError, ImapSettings, Mailbox, ReaderKeyword
from emi_reader.transport import ImaplibTransport
from unittests.emi_reader.reader_sims import make_email
from unittests.netfaults.fault_proxy import (
    BLACKHOLE,
    CLOSE_ON_CLIENT_WRITE,
    LATENCY,
    PASS,
    REFUSE,
    RESET_AFTER_BYTES,
    FaultProxy,
)
from unittests.netfaults.sim_imap_server import SimImapServer

_ADDRESS = "qa+400001_20261026t153012-w01-a1b2@example.com"
# Windows retries a refused connect for about 2 s before failing, so a shorter connect timeout would
# report a refused port as a connect timeout.
_REFUSE_CONNECT_SEC = 5.0


@pytest.fixture(name="proxy")
def _proxy() -> Iterator[FaultProxy]:
    """A simulated IMAP server holding one activation email for the address, behind a fault proxy."""
    with SimImapServer() as server, FaultProxy(server.port) as proxy:
        server.deliver(make_email(to_addr=_ADDRESS))
        yield proxy


def _mailbox(proxy: FaultProxy, connect_timeout: float = 1.0) -> Mailbox:
    """A reader that connects through the proxy with short timeouts."""
    return Mailbox(ImapSettings(port=proxy.port, connect_timeout=connect_timeout, read_timeout=1.0))


def _keywords(mailbox: Mailbox) -> list[str]:
    """The keywords of the retried attempts."""
    return [str(attempt["keyword"]) for attempt in mailbox.attempts]


@pytest.mark.network
class TestEMIReaderFaultProxy:
    """Each fault mode against the real client."""

    def EMI_READER_UNIT_100126_slow_network_still_finds_the_email(self, proxy: FaultProxy) -> None:
        """A delay on every packet, below the read timeout, costs time but nothing else."""
        proxy.set_mode(LATENCY, latency_sec=0.02)
        mailbox = _mailbox(proxy)
        assert "/activate?code=" in mailbox.wait_for_link(_ADDRESS, Cta.ACTIVATE_ACCOUNT, timeout=10.0)
        assert not mailbox.attempts

    def EMI_READER_UNIT_100127_silent_server_is_a_connect_timeout(self, proxy: FaultProxy) -> None:
        """A server that accepts and never greets: IMAP_CONNECTION_TIMEOUT at once, never retried."""
        proxy.set_mode(BLACKHOLE)
        mailbox = _mailbox(proxy)
        started = time.monotonic()
        with pytest.raises(ImapConnectionError) as caught:
            mailbox.wait_for_link(_ADDRESS, Cta.ACTIVATE_ACCOUNT, timeout=10.0)
        assert (caught.value.primary, caught.value.retryable) == (ReaderKeyword.IMAP_CONNECTION_TIMEOUT, False)
        assert time.monotonic() - started < 5.0 and not mailbox.attempts

    def EMI_READER_UNIT_100128_refused_port_is_fatal_until_it_listens_again(self, proxy: FaultProxy) -> None:
        """Nothing listening: IMAP_CONNECTION_REFUSED, never retried; once it listens, the wait works."""
        proxy.set_mode(REFUSE)
        with pytest.raises(ImapConnectionError) as caught:
            _mailbox(proxy, _REFUSE_CONNECT_SEC).wait_for_link(_ADDRESS, Cta.ACTIVATE_ACCOUNT, timeout=10.0)
        assert (caught.value.primary, caught.value.retryable) == (ReaderKeyword.IMAP_CONNECTION_REFUSED, False)
        proxy.set_mode(PASS)
        assert _mailbox(proxy, _REFUSE_CONNECT_SEC).wait_for_link(_ADDRESS, Cta.ACTIVATE_ACCOUNT, timeout=10.0)

    @pytest.mark.parametrize("after_bytes", [300, 1200], ids=["mid-status", "mid-fetch"])
    def EMI_READER_UNIT_100129_reset_mid_session_reconnects(self, proxy: FaultProxy, after_bytes: int) -> None:
        """A connection reset partway through a reply is retried on a new connection, which finds the email."""
        proxy.set_mode(RESET_AFTER_BYTES, after_bytes=after_bytes, once=True)
        mailbox = _mailbox(proxy)
        assert mailbox.wait_for_link(_ADDRESS, Cta.ACTIVATE_ACCOUNT, timeout=10.0)
        assert _keywords(mailbox) == [ReaderKeyword.IMAP_CONNECTION_DROPPED] and proxy.faults == 1

    def EMI_READER_UNIT_100130_closed_on_write_is_healable(self, proxy: FaultProxy) -> None:
        """A connection reset when the client writes is retryable on every system, then heals.

        The first command after the reset reads end-of-file (dropped); the next one fails to write (Windows
        10053/10054, Linux ``BrokenPipe`` or ``ConnectionReset``).
        """
        transport = ImaplibTransport(ImapSettings(port=proxy.port, connect_timeout=1.0, read_timeout=1.0))
        transport.open()
        proxy.set_mode(CLOSE_ON_CLIENT_WRITE)
        failures: list[ImapConnectionError] = []
        for _attempt in range(2):
            with pytest.raises(ImapConnectionError) as caught:
                transport.noop()
            failures.append(caught.value)
        assert failures[0].primary in (ReaderKeyword.IMAP_CONNECTION_DROPPED, ReaderKeyword.IMAP_SOCKET_WRITE_FAILED)
        assert failures[1].primary == ReaderKeyword.IMAP_SOCKET_WRITE_FAILED
        assert all(failure.retryable for failure in failures)
        transport.close()
        proxy.set_mode(CLOSE_ON_CLIENT_WRITE, after_writes=3, once=True)
        mailbox = _mailbox(proxy)
        assert mailbox.wait_for_link(_ADDRESS, Cta.ACTIVATE_ACCOUNT, timeout=10.0)
        assert len(mailbox.attempts) == 1

    def EMI_READER_UNIT_100131_reset_every_time_ends_with_dropped(self, proxy: FaultProxy) -> None:
        """A connection that keeps breaking is retried until the deadline, then reported as dropped."""
        proxy.set_mode(RESET_AFTER_BYTES, after_bytes=300)
        mailbox = _mailbox(proxy)
        with pytest.raises(ImapConnectionError) as caught:
            mailbox.wait_for_link(_ADDRESS, Cta.ACTIVATE_ACCOUNT, timeout=2.0)
        assert (caught.value.primary, caught.value.retryable) == (ReaderKeyword.IMAP_CONNECTION_DROPPED, True)
        assert len(mailbox.attempts) >= 2 and set(_keywords(mailbox)) == {ReaderKeyword.IMAP_CONNECTION_DROPPED}

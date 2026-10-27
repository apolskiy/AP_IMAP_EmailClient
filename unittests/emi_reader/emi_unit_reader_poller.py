# SPDX-FileCopyrightText: 2026 Aleksandr Polskiy
# SPDX-License-Identifier: Apache-2.0
"""Unit tests: one shared poller serves every waiting request (design §5.9, AMB-80)."""

import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

import pytest

from emi_reader import Cta, EmailWaitError, ImapConnectionError, ImapSettings, Mailbox, ReaderCaseKeyword
from emi_reader import LinkExtractionError, MailboxAccessError, ReaderKeyword
from emi_reader.matching import WaitTarget
from emi_reader.poller import SharedPoller, _search_new
from unittests.emi_reader.reader_sims import SimFolder, SimImap, make_email

_FAST = {"poll_interval": 0.02, "poll_jitter": 0.0, "backoff": (0.02,)}


def _address(slot: int) -> str:
    """A test address per waiting request."""
    return f"qa+c{400100 + slot}_20261027t120000-w00-abcd@example.com"


@pytest.fixture(name="pollers")
def _pollers() -> Iterator[list[SharedPoller]]:
    """Pollers made by a test, always closed afterwards (their threads end)."""
    made: list[SharedPoller] = []
    yield made
    for poller in made:
        poller.close()


def _poller(pollers: list[SharedPoller], server: SimImap, **options: Any) -> SharedPoller:
    """A fast poller over the simulated server."""
    poller = SharedPoller(ImapSettings(), transport_factory=server, **{**_FAST, **options})
    pollers.append(poller)
    return poller


def _in_threads(calls: list[Callable[[], Any]]) -> list[Any]:
    """Run the calls side by side; each one's result or raised error, in order."""
    results: list[Any] = [None] * len(calls)

    def _run(index: int) -> None:
        try:
            results[index] = calls[index]()
        except Exception as error:  # pylint: disable=broad-exception-caught
            results[index] = error

    threads = [threading.Thread(target=_run, args=(index,)) for index in range(len(calls))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)
    return results


class TestEMIReaderSharedPoller:
    """Several waits, one connection, one fetch per cycle."""

    def EMI_READER_UNIT_100136_one_connection_and_one_fetch_per_cycle(self, pollers: list[SharedPoller]) -> None:
        """Four waits on one poller: one connection, every email found, at most one fetch per cycle, and
        emails that arrive during the waits are found by one shared ``OR`` search over new UIDs."""
        server = SimImap()
        server.deliver("INBOX", make_email(to_addr="other@example.com"))  # an older email, not for the waits
        poller = _poller(pollers, server)
        mailboxes = [Mailbox(ImapSettings(), poller=poller) for _slot in range(4)]

        def _later() -> None:
            time.sleep(0.15)
            for slot in range(4):
                server.deliver("INBOX", make_email(to_addr=_address(slot)))

        threading.Thread(target=_later).start()
        links = _in_threads([lambda slot=slot: mailboxes[slot].wait_for_link(_address(slot), Cta.ACTIVATE_ACCOUNT,
                                                                             timeout=5.0) for slot in range(4)])
        assert all(isinstance(link, str) and "/activate" in link for link in links), links
        assert server.opens == 1
        assert server.calls.count("fetch_messages") <= poller.cycles
        assert any(criteria[2] == "OR" for criteria in server.searches if criteria[:1] == ("UID",))

    def EMI_READER_UNIT_100137_or_search_groups_terms(self) -> None:
        """Every waiting address on each of its fields, ``OR``-joined, at most 20 terms per search."""
        server = SimImap()
        server.deliver("INBOX", make_email(to_addr="a@example.com"))
        server.deliver("INBOX", make_email(cc_addr="b@example.com"))
        server.open()
        server.examine("INBOX")
        targets = [WaitTarget("a@example.com", None, None, None, ("to",)),
                   WaitTarget("b@example.com", None, None, None, ("to", "cc"))]
        assert _search_new(server, targets, 1) == {1, 2}  # type: ignore[arg-type]
        assert server.searches[-1] == ("UID", "1:*", "OR", "TO", '"a@example.com"', "OR", "TO", '"b@example.com"',
                                       "CC", '"b@example.com"')
        many = [WaitTarget(f"x{index}@example.com", None, None, None, ("to",)) for index in range(45)]
        _search_new(server, many, 1)  # type: ignore[arg-type]
        assert [len([term for term in criteria if term == "TO"]) for criteria in server.searches[-3:]] == [20, 20, 5]

    def EMI_READER_UNIT_100138_each_wait_keeps_its_own_rules(self, pollers: list[SharedPoller]) -> None:
        """A wait for an email that never comes ends at its own deadline with CASE_EMAIL_NOT_RECEIVED, while
        a wait for two emails on the same poller returns both."""
        server = SimImap()
        server.deliver("INBOX", make_email(to_addr=_address(1)))
        server.deliver("INBOX", make_email(to_addr=_address(1), path="/activate2"))
        poller = _poller(pollers, server)
        missing = Mailbox(ImapSettings(), poller=poller)
        pair = Mailbox(ImapSettings(), poller=poller)
        results = _in_threads([
            lambda: missing.wait_for_link(_address(0), Cta.ACTIVATE_ACCOUNT, timeout=0.3),
            lambda: pair.wait_for_emails(_address(1), count=2, timeout=5.0, settle_window=0.05),
        ])
        assert isinstance(results[0], EmailWaitError) and results[0].primary == ReaderCaseKeyword.EMAIL_NOT_RECEIVED
        assert len(results[1]) == 2
        assert pair.inventory is not None and pair.inventory["after"]["categories"]["activation"] == 2

    @pytest.mark.network
    def EMI_READER_UNIT_100139_network_errors_retried_for_every_wait(self, pollers: list[SharedPoller]) -> None:
        """A dropped connection is retried by the poller and shows in each wait's attempts; a refused first
        connect ends every waiting request at once (AMB-13)."""
        server = SimImap()
        server.deliver("INBOX", make_email(to_addr=_address(0)))
        server.failures["examine"] = [ImapConnectionError("reset", ReaderKeyword.IMAP_CONNECTION_DROPPED,
                                                          retryable=True)]
        poller = _poller(pollers, server)
        mailbox = Mailbox(ImapSettings(), poller=poller)
        assert mailbox.wait_for_link(_address(0), Cta.ACTIVATE_ACCOUNT, timeout=5.0)
        assert [attempt["keyword"] for attempt in mailbox.attempts] == [ReaderKeyword.IMAP_CONNECTION_DROPPED]
        down = SimImap()
        down.failures["open"] = [ImapConnectionError("refused", ReaderKeyword.IMAP_CONNECTION_REFUSED,
                                                     infrastructure=True)]
        refused = Mailbox(ImapSettings(), poller=_poller(pollers, down))
        started = time.monotonic()
        with pytest.raises(ImapConnectionError) as caught:
            refused.wait_for_link(_address(0), Cta.ACTIVATE_ACCOUNT, timeout=10.0)
        assert caught.value.primary == ReaderKeyword.IMAP_CONNECTION_REFUSED and time.monotonic() - started < 5

    @pytest.mark.network
    def EMI_READER_UNIT_100140_refused_reconnect_retried_after_a_session(self, pollers: list[SharedPoller]) -> None:
        """After a working cycle, a dropped session and a refused reconnect (a restart) are retried until the
        email is found (AMB-78, on the poller's connection)."""
        server = SimImap()
        poller = _poller(pollers, server)
        mailbox = Mailbox(ImapSettings(), poller=poller)

        def _restart() -> None:
            time.sleep(0.1)
            server.failures["examine"] = [ImapConnectionError("reset", ReaderKeyword.IMAP_CONNECTION_DROPPED,
                                                              retryable=True)]
            server.failures["open"] = [ImapConnectionError("refused", ReaderKeyword.IMAP_CONNECTION_REFUSED,
                                                           infrastructure=True)]
            time.sleep(0.1)
            server.deliver("INBOX", make_email(to_addr=_address(0)))

        threading.Thread(target=_restart).start()
        assert mailbox.wait_for_link(_address(0), Cta.ACTIVATE_ACCOUNT, timeout=5.0)
        keywords = [attempt["keyword"] for attempt in mailbox.attempts]
        assert keywords[:2] == [ReaderKeyword.IMAP_CONNECTION_DROPPED, ReaderKeyword.IMAP_CONNECTION_REFUSED]

    @pytest.mark.network
    def EMI_READER_UNIT_100141_no_free_slot_is_connection_limit(self, pollers: list[SharedPoller]) -> None:
        """The guard (the broker's budget) never frees a slot: the wait retries and ends at its deadline with
        READER_IMAP_CONNECTION_LIMIT, the network keyword, not CASE_EMAIL_NOT_RECEIVED."""

        @contextmanager
        def _no_slot() -> Iterator[None]:
            raise TimeoutError("busy")
            yield  # pylint: disable=unreachable

        poller = _poller(pollers, SimImap(), guard=_no_slot)
        with pytest.raises(ImapConnectionError) as caught:
            Mailbox(ImapSettings(), poller=poller).wait_for_link(_address(0), Cta.ACTIVATE_ACCOUNT, timeout=0.3)
        assert caught.value.primary == ReaderKeyword.IMAP_CONNECTION_LIMIT

    def EMI_READER_UNIT_100142_broken_link_ends_only_its_wait(self, pollers: list[SharedPoller]) -> None:
        """An email whose HTML and text links differ fails its own wait; another wait on the poller succeeds."""
        server = SimImap()
        server.deliver("INBOX", make_email(to_addr=_address(0), path="/activate/", text_path="/activate"))
        server.deliver("INBOX", make_email(to_addr=_address(1)))
        poller = _poller(pollers, server)
        results = _in_threads([lambda slot=slot: Mailbox(ImapSettings(), poller=poller).wait_for_link(
            _address(slot), Cta.ACTIVATE_ACCOUNT, timeout=5.0) for slot in range(2)])
        assert isinstance(results[0], LinkExtractionError) and isinstance(results[1], str)

    def EMI_READER_UNIT_100145_unreadable_expected_folder_ends_only_its_wait(self, pollers: list[SharedPoller]) -> None:
        """A wait naming an unreadable folder ends at once with the expected-folder keyword; another wait on
        the same poller finds its email in the readable folder (AMB-85)."""
        settings = ImapSettings(folders=("INBOX", "Spam"))
        server = SimImap()
        server.folders["Spam"] = SimFolder(selectable=False)
        server.deliver("INBOX", make_email(to_addr=_address(1)))
        poller = SharedPoller(settings, transport_factory=server, **_FAST)
        pollers.append(poller)
        started = time.monotonic()
        results = _in_threads([
            lambda: Mailbox(settings, poller=poller).wait_for_link(_address(0), Cta.ACTIVATE_ACCOUNT, timeout=10.0,
                                                                   expected_folder="Spam"),
            lambda: Mailbox(settings, poller=poller).wait_for_link(_address(1), Cta.ACTIVATE_ACCOUNT, timeout=5.0),
        ])
        assert isinstance(results[0], MailboxAccessError), results
        assert results[0].primary == ReaderKeyword.MAILBOX_EXPECTED_FOLDER_INACCESSIBLE
        assert isinstance(results[1], str) and time.monotonic() - started < 5

    def EMI_READER_UNIT_100143_idle_poller_closes_and_restarts(self, pollers: list[SharedPoller]) -> None:
        """With no request the connection closes after the idle time; the next request reconnects."""
        server = SimImap()
        server.deliver("INBOX", make_email(to_addr=_address(0)))
        poller = _poller(pollers, server, idle_close_sec=0.05)
        mailbox = Mailbox(ImapSettings(), poller=poller)
        assert mailbox.wait_for_link(_address(0), Cta.ACTIVATE_ACCOUNT, timeout=5.0)
        deadline = time.monotonic() + 5
        while server.calls[-1:] != ["close"] and time.monotonic() < deadline:
            time.sleep(0.02)
        assert mailbox.wait_for_link(_address(0), Cta.ACTIVATE_ACCOUNT, timeout=5.0)
        assert server.opens == 2
        poller.close()
        with pytest.raises(RuntimeError):
            poller.register(WaitTarget(_address(0), None, None, None, ("to",)), None)  # type: ignore[arg-type]

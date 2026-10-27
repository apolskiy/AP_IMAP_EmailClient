# SPDX-FileCopyrightText: 2026 Aleksandr Polskiy
# SPDX-License-Identifier: Apache-2.0
"""Unit tests: the tools while the real Greenmail container is down (design §13.2, layer 3, AMB-77).

Each test stops Greenmail, checks what the tool reports, and starts it again (the fixture also restarts
it if a test fails half-way). A restart empties the mailbox.
"""

import threading
import time

import pytest

from emi_api import ClientMode, EmiApi, EmiSettings
from emi_greenmail import GreenmailKeyword
from emi_reader import Cta, ImapConnectionError, Mailbox, ReaderKeyword
from emi_sender import Sender, SenderKeyword
from unittests.outage.greenmail_container import GreenmailContainer

pytestmark = pytest.mark.docker

_RUN = "20261026t153012-w01-a1b2"
_ADDRESS = f"qa+c400900_{_RUN}@example.com"
_OUTAGE_KEYWORDS = (ReaderKeyword.IMAP_CONNECTION_REFUSED, ReaderKeyword.IMAP_CONNECTION_DROPPED,
                    ReaderKeyword.IMAP_CONNECTION_TIMEOUT, ReaderKeyword.IMAP_READ_TIMEOUT)


def _mailbox() -> Mailbox:
    """A reader with the environment's settings (``EMI_IMAP_PASSWORD`` and the rest)."""
    return Mailbox(EmiSettings.from_env().imap)


def _sender() -> Sender:
    """A sender with the environment's settings (``EMI_SMTP_PASSWORD`` and the rest)."""
    return Sender(EmiSettings.from_env().smtp)


class TestEMIOutageGreenmail:
    """Greenmail stopped and started under the reader, the sender and the API."""

    def EMI_READER_UNIT_100132_wait_while_down_is_a_network_failure(self, greenmail: GreenmailContainer) -> None:
        """A wait with Greenmail down fails with a network keyword at once, never as an email not received."""
        greenmail.stop()
        started = time.monotonic()
        with pytest.raises(ImapConnectionError) as caught:
            _mailbox().wait_for_link(_ADDRESS, Cta.ACTIVATE_ACCOUNT, timeout=30.0)
        assert caught.value.primary in _OUTAGE_KEYWORDS and caught.value.infrastructure
        assert time.monotonic() - started < 25.0

    def EMI_READER_UNIT_100133_wait_rides_through_a_restart(self, greenmail: GreenmailContainer) -> None:
        """Greenmail stopped and started during a wait: the reconnects are retried inside the deadline
        (AMB-78), and the wait finds the email sent after the restart."""
        outcome: list[object] = []
        mailbox = _mailbox()

        def _wait() -> None:
            try:
                outcome.append(mailbox.wait_for_link(_ADDRESS, Cta.ACTIVATE_ACCOUNT, timeout=120.0))
            except ImapConnectionError as error:
                outcome.append(error)

        waiting = threading.Thread(target=_wait)
        waiting.start()
        time.sleep(2.0)  # the wait is polling
        greenmail.stop()
        time.sleep(3.0)  # the wait is reconnecting and being refused
        greenmail.start()
        _sender().send("activation", _ADDRESS)
        waiting.join(timeout=120.0)
        assert outcome and isinstance(outcome[0], str) and "/activate" in outcome[0], outcome
        assert {str(attempt["keyword"]) for attempt in mailbox.attempts} <= set(_OUTAGE_KEYWORDS)
        assert mailbox.attempts

    def EMI_SENDER_UNIT_110069_send_while_down_exits_3_without_retry(self, greenmail: GreenmailContainer) -> None:
        """A send with Greenmail down fails at connect for every email, exit code 3, not retried.

        Docker on Linux refuses the port (SENDER_SMTP_CONNECTION_REFUSED); Docker Desktop on Windows still
        accepts it and closes it at once (SENDER_SMTP_SERVER_UNREACHABLE). Both are fatal.
        """
        greenmail.stop()
        manifest = _sender().send("activation", _ADDRESS, 2)
        keywords = {entry.keyword for entry in manifest.entries}
        assert manifest.exit_code == 3 and len(keywords) == 1
        assert keywords <= {SenderKeyword.SMTP_CONNECTION_REFUSED, SenderKeyword.SMTP_SERVER_UNREACHABLE}
        assert all(entry.attempts <= 1 for entry in manifest.entries)

    def EMI_API_UNIT_300064_health_follows_greenmail(self, greenmail: GreenmailContainer) -> None:
        """api.health() says GREENMAIL_NOT_READY while Greenmail is down, never raises, and is ready again
        after the restart."""
        with EmiApi(EmiSettings.from_env(mode=ClientMode.INPROCESS), run_id=_RUN) as api:
            greenmail.stop()
            down = api.health()
            assert not down.ready and down.problems[0].component == "greenmail"
            assert GreenmailKeyword.NOT_READY in (down.keywords[0], *down.keywords[1])
            greenmail.start()
            assert api.health().ready

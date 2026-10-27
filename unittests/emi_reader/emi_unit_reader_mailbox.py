# SPDX-FileCopyrightText: 2026 Aleksandr Polskiy
# SPDX-License-Identifier: Apache-2.0
"""Unit tests: waiting for a test's email (design §5.4, §5.5) against a simulated IMAP server."""

import random

import pytest

from emi_reader import (
    Cta,
    EmailWaitError,
    ImapConnectionError,
    ImapSettings,
    Mailbox,
    MailboxAccessError,
    ReaderCaseKeyword,
    ReaderKeyword,
    WaitOptions,
)
from unittests.emi_reader.reader_sims import SimImap, make_email
from unittests.sims import SimClock

_ADDRESS = "qa+400001_20261026t153012-w01-a1b2@example.com"


def _mailbox(
    server: SimImap, clock: SimClock, folders: tuple[str, ...] = ("INBOX",), **options: object
) -> Mailbox:
    """Build a mailbox wired to the simulated server and clock."""
    return Mailbox(
        ImapSettings(folders=folders),
        options=WaitOptions(**options),  # type: ignore[arg-type]
        transport_factory=server,
        clock=clock.now,
        sleeper=clock.sleep,
        rng=random.Random(7),
    )


def _drop() -> ImapConnectionError:
    """A healable mid-session fault."""
    return ImapConnectionError("dropped", ReaderKeyword.IMAP_CONNECTION_DROPPED, retryable=True,
                               infrastructure=True)


class TestEMIReaderWait:
    """Finding the right email, before and during the wait."""

    def EMI_READER_UNIT_100060_finds_pre_arrived_email(self) -> None:
        """Mail that arrived before the wait started is found at once, without sleeping."""
        clock = SimClock()
        server = SimImap(clock)
        server.deliver("INBOX", make_email(to_addr=_ADDRESS))
        link = _mailbox(server, clock).wait_for_link(_ADDRESS, Cta.ACTIVATE_ACCOUNT)
        assert "/activate?code=" in link
        assert not clock.sleeps

    def EMI_READER_UNIT_100061_finds_email_arriving_mid_wait(self) -> None:
        """Mail arriving during the wait is picked up by a later poll."""
        clock = SimClock()
        server = SimImap(clock)
        server.arrivals.append((5.0, "INBOX", make_email(to_addr=_ADDRESS)))
        _mailbox(server, clock).wait_for_link(_ADDRESS, Cta.ACTIVATE_ACCOUNT)
        assert 5.0 <= clock.now() < 10.0

    def EMI_READER_UNIT_100062_ignores_other_addresses_and_prefixes(self) -> None:
        """Mail to a longer address sharing a prefix, or to another run, is never matched."""
        clock = SimClock()
        server = SimImap(clock)
        server.deliver("INBOX", make_email(to_addr=_ADDRESS.replace("a1b2@", "a1b2x@")))
        server.deliver("INBOX", make_email(to_addr=_ADDRESS.replace("w01", "w02")))
        with pytest.raises(EmailWaitError) as caught:
            _mailbox(server, clock).wait_for_link(_ADDRESS, Cta.ACTIVATE_ACCOUNT, timeout=6.0)
        assert caught.value.primary == ReaderCaseKeyword.EMAIL_NOT_RECEIVED

    def EMI_READER_UNIT_100063_other_kind_to_same_address_is_skipped(self) -> None:
        """An invitation to the same address does not satisfy a wait for activation."""
        clock = SimClock()
        server = SimImap(clock)
        invitation = make_email(to_addr=_ADDRESS, label="Accept invitation", path="/invite/accept")
        server.deliver("INBOX", invitation)
        server.arrivals.append((3.0, "INBOX", make_email(to_addr=_ADDRESS)))
        link = _mailbox(server, clock).wait_for_link(_ADDRESS, Cta.ACTIVATE_ACCOUNT)
        assert "/activate?" in link

    def EMI_READER_UNIT_100064_marker_must_match(self) -> None:
        """With markers requested, an email of another test or run is ignored."""
        clock = SimClock()
        server = SimImap(clock)
        server.deliver("INBOX", make_email(to_addr=_ADDRESS, test_id="400001", run_id="other-run"))
        mine = make_email(to_addr=_ADDRESS, test_id="400001", run_id="my-run")
        server.arrivals.append((2.0, "INBOX", mine))
        found = _mailbox(server, clock).wait_for_emails(_ADDRESS, test_id="400001", run_id="my-run")
        assert [item.email.run_id for item in found] == ["my-run"]

    @pytest.mark.parametrize("wanted", ["C400001", "c400001", "400001"])
    def EMI_READER_UNIT_100114_test_id_matches_in_testrail_form(self, wanted: str) -> None:
        """A marker C400001 matches the TestRail case ID in any accepted form; another case does not."""
        clock = SimClock()
        server = SimImap(clock)
        server.deliver("INBOX", make_email(to_addr=_ADDRESS, test_id="C400002", run_id="my-run"))
        server.deliver("INBOX", make_email(to_addr=_ADDRESS, test_id="C400001", run_id="my-run"))
        found = _mailbox(server, clock).wait_for_emails(_ADDRESS, test_id=wanted, run_id="my-run")
        assert [item.email.test_id for item in found] == ["C400001"]

    def EMI_READER_UNIT_100065_never_changes_the_mailbox(self) -> None:
        """Only read-only operations are used."""
        clock = SimClock()
        server = SimImap(clock)
        server.deliver("INBOX", make_email(to_addr=_ADDRESS))
        _mailbox(server, clock).wait_for_link(_ADDRESS, Cta.ACTIVATE_ACCOUNT)
        assert set(server.calls) <= {"open", "list_folders", "examine", "status", "uid_search",
                                     "fetch_headers", "fetch_message", "noop"}


class TestEMIReaderRecipientFields:
    """``To`` by default; other fields only when selected (design §5.4)."""

    def EMI_READER_UNIT_100070_cc_only_not_matched_by_default(self) -> None:
        """Address only in Cc: not found with the default To-only matching."""
        clock = SimClock()
        server = SimImap(clock)
        server.deliver("INBOX", make_email(to_addr="someone@example.com", cc_addr=_ADDRESS))
        with pytest.raises(EmailWaitError):
            _mailbox(server, clock).wait_for_emails(_ADDRESS, timeout=4.0)

    def EMI_READER_UNIT_100071_cc_matched_when_selected(self) -> None:
        """Address only in Cc: found when Cc is a selected field."""
        clock = SimClock()
        server = SimImap(clock)
        server.deliver("INBOX", make_email(to_addr="someone@example.com", cc_addr=_ADDRESS))
        found = _mailbox(server, clock, match_fields=("to", "cc")).wait_for_emails(_ADDRESS, timeout=4.0)
        assert len(found) == 1

    def EMI_READER_UNIT_100072_unknown_field_rejected(self) -> None:
        """A recipient field the reader does not know is a configuration error."""
        with pytest.raises(ValueError):
            WaitOptions(match_fields=("reply-to",))

    def EMI_READER_UNIT_100115_fields_chosen_per_wait(self) -> None:
        """One mailbox (To-only by default) matches Cc for a single wait when that wait asks for it."""
        clock = SimClock()
        server = SimImap(clock)
        server.deliver("INBOX", make_email(to_addr="someone@example.com", cc_addr=_ADDRESS))
        mailbox = _mailbox(server, clock)
        assert len(mailbox.wait_for_emails(_ADDRESS, timeout=4.0, match_fields=("CC",))) == 1
        assert "/activate?" in mailbox.wait_for_link(_ADDRESS, Cta.ACTIVATE_ACCOUNT, match_fields=("cc",))
        with pytest.raises(EmailWaitError):
            mailbox.wait_for_emails(_ADDRESS, timeout=4.0)

    def EMI_READER_UNIT_100117_settle_window_per_wait(self) -> None:
        """settle_window=0 returns as soon as the count is met; the default keeps watching 2 s; negative refused."""
        clock = SimClock()
        server = SimImap(clock)
        server.deliver("INBOX", make_email(to_addr=_ADDRESS))
        mailbox = _mailbox(server, clock)
        assert len(mailbox.wait_for_emails(_ADDRESS, settle_window=0.0)) == 1 and clock.now() == 0.0
        mailbox.wait_for_emails(_ADDRESS)
        assert clock.now() >= 2.0
        with pytest.raises(ValueError, match="settle_window"):
            mailbox.wait_for_emails(_ADDRESS, settle_window=-1.0)

    @pytest.mark.parametrize("fields", [(), ("reply-to",)])
    def EMI_READER_UNIT_100116_per_wait_fields_validated(self, fields: tuple[str, ...]) -> None:
        """Empty or unknown per-wait fields are refused before anything is searched."""
        clock = SimClock()
        server = SimImap(clock)
        with pytest.raises(ValueError, match="match_fields"):
            _mailbox(server, clock).wait_for_emails(_ADDRESS, timeout=4.0, match_fields=fields)
        assert not server.calls


class TestEMIReaderCounts:
    """Exact counts, duplicates and the settle window."""

    def EMI_READER_UNIT_100080_count_exceeded_fails(self) -> None:
        """A third email arriving within the settle window fails a wait for two."""
        clock = SimClock()
        server = SimImap(clock)
        for _index in range(2):
            server.deliver("INBOX", make_email(to_addr=_ADDRESS))
        server.arrivals.append((1.0, "INBOX", make_email(to_addr=_ADDRESS)))
        with pytest.raises(EmailWaitError) as caught:
            _mailbox(server, clock, poll_interval=0.5, poll_jitter=0.0).wait_for_emails(_ADDRESS, count=2)
        assert caught.value.primary == ReaderCaseKeyword.EMAIL_COUNT_EXCEEDED

    def EMI_READER_UNIT_100081_exact_count_passes_after_settle(self) -> None:
        """Exactly N emails: the wait returns them after the settle window."""
        clock = SimClock()
        server = SimImap(clock)
        for _index in range(3):
            server.deliver("INBOX", make_email(to_addr=_ADDRESS))
        found = _mailbox(server, clock).wait_for_emails(_ADDRESS, count=3)
        assert len(found) == 3
        assert clock.now() >= 2.0

    def EMI_READER_UNIT_100082_fewer_than_expected_is_not_received(self) -> None:
        """Two of three emails by the deadline: not received, with the count found."""
        clock = SimClock()
        server = SimImap(clock)
        for _index in range(2):
            server.deliver("INBOX", make_email(to_addr=_ADDRESS))
        with pytest.raises(EmailWaitError) as caught:
            _mailbox(server, clock).wait_for_emails(_ADDRESS, count=3, timeout=5.0)
        assert caught.value.primary == ReaderCaseKeyword.EMAIL_NOT_RECEIVED
        assert caught.value.details["found"] == 2

    def EMI_READER_UNIT_100083_count_below_one_rejected(self) -> None:
        """A wait for zero or a negative number of emails is invalid input."""
        with pytest.raises(ValueError):
            _mailbox(SimImap(), SimClock()).wait_for_emails(_ADDRESS, count=0)


class TestEMIReaderNetworkFaults:
    """Retry only what retrying can fix; report outages as outages (design §5.5)."""

    def EMI_READER_UNIT_100090_drop_is_retried_and_wait_succeeds(self) -> None:
        """A dropped connection mid-wait reconnects and still finds the email."""
        clock = SimClock()
        server = SimImap(clock)
        server.arrivals.append((3.0, "INBOX", make_email(to_addr=_ADDRESS)))
        server.failures["examine"] = [_drop()]
        mailbox = _mailbox(server, clock)
        assert mailbox.wait_for_link(_ADDRESS, Cta.ACTIVATE_ACCOUNT)
        assert server.opens == 2
        assert [attempt["keyword"] for attempt in mailbox.attempts] == [ReaderKeyword.IMAP_CONNECTION_DROPPED]

    def EMI_READER_UNIT_100091_server_down_fails_at_once(self) -> None:
        """Connection refused is not retried: the error surfaces immediately with its own keyword."""
        clock = SimClock()
        server = SimImap(clock)
        server.failures["open"] = [ImapConnectionError("refused", ReaderKeyword.IMAP_CONNECTION_REFUSED,
                                                       infrastructure=True)]
        with pytest.raises(ImapConnectionError) as caught:
            _mailbox(server, clock).wait_for_link(_ADDRESS, Cta.ACTIVATE_ACCOUNT)
        assert caught.value.primary == ReaderKeyword.IMAP_CONNECTION_REFUSED
        assert caught.value.infrastructure is True
        assert not clock.sleeps

    def EMI_READER_UNIT_100094_no_readable_folder_names_the_causes(self) -> None:
        """Connected, but every configured folder is unreadable: environment failure with details."""
        clock = SimClock()
        server = SimImap(clock)
        server.folders["INBOX"].selectable = False
        mailbox = _mailbox(server, clock, folders=("INBOX", "Missing"))
        with pytest.raises(MailboxAccessError) as caught:
            mailbox.wait_for_link(_ADDRESS, Cta.ACTIVATE_ACCOUNT)
        assert caught.value.primary == ReaderKeyword.MAILBOX_NONE_ACCESSIBLE
        assert caught.value.infrastructure is True
        assert {row["state"] for row in mailbox.access_matrix} == {"select_denied", "not_listed"}

    def EMI_READER_UNIT_100092_outage_until_deadline_reports_network_keyword(self) -> None:
        """If the last attempt failed on the network, the outage is primary, not 'not received'."""
        clock = SimClock()
        server = SimImap(clock)
        server.failures["examine"] = [_drop() for _index in range(100)]
        with pytest.raises(ImapConnectionError) as caught:
            _mailbox(server, clock).wait_for_link(_ADDRESS, Cta.ACTIVATE_ACCOUNT, timeout=10.0)
        assert caught.value.primary == ReaderKeyword.IMAP_CONNECTION_DROPPED
        assert caught.value.details["attempts"]

    def EMI_READER_UNIT_100093_backoff_is_capped(self) -> None:
        """Reconnect delays follow 0.5, 1, 2, 4, 5 and then stay at 5 seconds."""
        clock = SimClock()
        server = SimImap(clock)
        server.failures["examine"] = [_drop() for _index in range(100)]
        with pytest.raises(ImapConnectionError):
            _mailbox(server, clock).wait_for_link(_ADDRESS, Cta.ACTIVATE_ACCOUNT, timeout=30.0)
        assert clock.sleeps[:7] == [0.5, 1.0, 2.0, 4.0, 5.0, 5.0, 5.0]


class TestEMIReaderFolders:
    """Several folders, unreadable folders and filtering (design §5.3)."""

    def EMI_READER_UNIT_100100_searches_every_readable_folder(self) -> None:
        """An email in a second readable folder is found; a missing folder does not stop the wait."""
        clock = SimClock()
        server = SimImap(clock)
        server.deliver("Updates", make_email(to_addr=_ADDRESS))
        mailbox = _mailbox(server, clock, folders=("INBOX", "Updates", "Missing"))
        found = mailbox.wait_for_emails(_ADDRESS)
        assert found[0].folder == "Updates"
        assert {row["state"] for row in mailbox.access_matrix} == {"accessible", "not_listed"}

    def EMI_READER_UNIT_100101_filtered_to_unreadable_folder_is_environment_failure(self) -> None:
        """Not found + new mail in a denied folder: environment failure, not a test failure."""
        clock = SimClock()
        server = SimImap(clock)
        server.deliver("Junk", b"Subject: old\n\n")
        server.folders["Junk"].selectable = False
        server.arrivals.append((2.0, "Junk", make_email(to_addr=_ADDRESS)))
        with pytest.raises(MailboxAccessError) as caught:
            _mailbox(server, clock, folders=("INBOX", "Junk")).wait_for_link(
                _ADDRESS, Cta.ACTIVATE_ACCOUNT, timeout=5.0)
        assert caught.value.primary == ReaderKeyword.MAILBOX_FILTERED_TO_INACCESSIBLE
        assert caught.value.infrastructure is True

    def EMI_READER_UNIT_100102_uidvalidity_change_triggers_new_search(self) -> None:
        """A recreated folder is searched again instead of trusting old UIDs."""
        clock = SimClock()
        server = SimImap(clock)
        mailbox = _mailbox(server, clock)
        server.arrivals.append((1.0, "INBOX", b"Subject: noise\n\n"))
        def _recreate() -> None:
            server.folders["INBOX"].uidvalidity = 2
            server.deliver("INBOX", make_email(to_addr=_ADDRESS))
        original_sleep = clock.sleep
        def _sleep(seconds: float) -> None:
            original_sleep(seconds)
            if server.folders["INBOX"].uidvalidity == 1 and clock.now() > 3.0:
                _recreate()
        mailbox = Mailbox(ImapSettings(), transport_factory=server, clock=clock.now, sleeper=_sleep)
        assert mailbox.wait_for_link(_ADDRESS, Cta.ACTIVATE_ACCOUNT)

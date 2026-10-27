# SPDX-FileCopyrightText: 2026 Aleksandr Polskiy
# SPDX-License-Identifier: Apache-2.0
"""Unit tests: network-fault classification (layer 1 of design §13.2) and folder accessibility."""

import imaplib
import socket
import ssl

import pytest

from emi_reader import ReaderKeyword
from emi_reader.accessibility import Access, filter_evidence, probe_account
from emi_reader.errors import ImapConnectionError
from emi_reader.transport import FolderStatus, Phase, classify
from unittests.emi_reader.reader_sims import SimImap


def _wrapped_abort(cause: OSError) -> imaplib.IMAP4.abort:
    """Reproduce how imaplib reports a failed socket write: abort with the OSError as context."""
    abort_error = imaplib.IMAP4.abort(f"socket error: {cause}")
    abort_error.__context__ = cause
    return abort_error


_CASES = [
    (ConnectionRefusedError(10061, "refused"), Phase.CONNECT, ReaderKeyword.IMAP_CONNECTION_REFUSED, False),
    (socket.gaierror(11001, "getaddrinfo failed"), Phase.CONNECT, ReaderKeyword.IMAP_DNS_FAILURE, False),
    (TimeoutError("timed out"), Phase.CONNECT, ReaderKeyword.IMAP_CONNECTION_TIMEOUT, False),
    (TimeoutError("timed out"), Phase.COMMAND, ReaderKeyword.IMAP_READ_TIMEOUT, True),
    (ssl.SSLCertVerificationError("bad cert"), Phase.CONNECT, ReaderKeyword.IMAP_TLS_HANDSHAKE_FAILED, False),
    (imaplib.IMAP4.error("[AUTHENTICATIONFAILED] Invalid credentials"), Phase.LOGIN,
     ReaderKeyword.IMAP_AUTH_FAILED, False),
    (imaplib.IMAP4.abort("socket error: EOF"), Phase.COMMAND, ReaderKeyword.IMAP_CONNECTION_DROPPED, True),
    (ConnectionResetError(10054, "reset by peer"), Phase.COMMAND,
     ReaderKeyword.IMAP_CONNECTION_DROPPED, True),
    (_wrapped_abort(BrokenPipeError(32, "broken pipe")), Phase.COMMAND,
     ReaderKeyword.IMAP_SOCKET_WRITE_FAILED, True),
    (_wrapped_abort(ConnectionAbortedError(10053, "aborted")), Phase.COMMAND,
     ReaderKeyword.IMAP_SOCKET_WRITE_FAILED, True),
    (imaplib.IMAP4.error("[ALERT] Too many simultaneous connections"), Phase.LOGIN,
     ReaderKeyword.IMAP_CONNECTION_LIMIT, True),
    (imaplib.IMAP4.error("BAD unknown command"), Phase.COMMAND, ReaderKeyword.IMAP_PROTOCOL_ERROR, False),
]


class TestEMIReaderFaultClassification:
    """Every low-level failure gets the right keyword and retry decision (design §5.5)."""

    @pytest.mark.parametrize(("error", "phase", "keyword", "retryable"), _CASES)
    @pytest.mark.network
    def EMI_READER_UNIT_100040_classifies_fault(
        self, error: BaseException, phase: Phase, keyword: ReaderKeyword, retryable: bool
    ) -> None:
        """Server down, auth and TLS faults are fatal; mid-session faults are retryable."""
        classified = classify(error, phase, "127.0.0.1:3143")
        assert classified.primary == keyword
        assert classified.retryable is retryable
        assert classified.infrastructure is True
        assert classified.details["endpoint"] == "127.0.0.1:3143"

    @pytest.mark.network
    def EMI_READER_UNIT_100041_keeps_original_socket_error(self) -> None:
        """The errno hidden inside imaplib's abort is reported."""
        classified = classify(_wrapped_abort(ConnectionAbortedError(10053, "aborted")), Phase.COMMAND, "h:1")
        assert classified.details["error_type"] == "ConnectionAbortedError"
        assert classified.details["errno"] == 10053


class TestEMIReaderAccessibility:
    """Folder probe and filter evidence (design §5.3)."""

    def EMI_READER_UNIT_100050_classifies_each_folder(self) -> None:
        """Accessible, not listed, noselect and select-denied folders are told apart."""
        server = SimImap()
        server.deliver("Junk", b"Subject: x\n\n")
        server.folders["Junk"].selectable = False
        server.deliver("Archive", b"Subject: x\n\n")
        server.folders["Archive"].flags = frozenset({"\\noselect"})
        states = {entry.folder: entry.state
                  for entry in probe_account(server, "qa", ("INBOX", "Junk", "Archive", "Missing"))}
        assert states == {"INBOX": Access.ACCESSIBLE, "Junk": Access.SELECT_DENIED,
                          "Archive": Access.NOSELECT, "Missing": Access.NOT_LISTED}

    @pytest.mark.parametrize(("keyword", "state"), [
        (ReaderKeyword.IMAP_AUTH_FAILED, Access.LOGIN_FAILED),
        (ReaderKeyword.IMAP_CONNECTION_REFUSED, Access.NETWORK_ERROR),
    ])
    @pytest.mark.network
    def EMI_READER_UNIT_100051_login_and_network_failures_mark_all_folders(
        self, keyword: ReaderKeyword, state: Access
    ) -> None:
        """A failed login or connection makes every folder of that account inaccessible."""
        server = SimImap()
        server.failures["open"] = [ImapConnectionError("x", keyword)]
        entries = probe_account(server, "qa", ("INBOX", "Junk"))
        assert {entry.state for entry in entries} == {state}

    def EMI_READER_UNIT_100052_filter_evidence_signals(self) -> None:
        """Declared folder, \\Junk flag and UIDNEXT growth each count as evidence."""
        server = SimImap()
        server.deliver("Spam", b"Subject: x\n\n")
        server.folders["Spam"].selectable = False
        server.folders["Spam"].flags = frozenset({"\\junk"})
        before = probe_account(server, "qa", ("INBOX", "Spam"))
        server.deliver("Spam", b"Subject: y\n\n")
        after = probe_account(server, "qa", ("INBOX", "Spam"))
        signals = filter_evidence(before, after, ("spam",))
        assert len(signals) == 3
        assert filter_evidence(before, before, ()) == ["\\Junk folder qa/Spam is select_denied"]

    def EMI_READER_UNIT_100053_status_parsing_shape(self) -> None:
        """FolderStatus carries the three counters the wait relies on."""
        status = FolderStatus(messages=3, uidnext=4, uidvalidity=7)
        assert (status.messages, status.uidnext, status.uidvalidity) == (3, 4, 7)

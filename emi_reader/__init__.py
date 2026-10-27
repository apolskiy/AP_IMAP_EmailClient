# SPDX-FileCopyrightText: 2026 Aleksandr Polskiy
# SPDX-License-Identifier: Apache-2.0
"""emi_reader: wait for a test's email over IMAP and extract its call-to-action link."""

from emi_reader.errors import (
    EmailWaitError,
    ImapConnectionError,
    LinkExtractionError,
    MailboxAccessError,
    MimeDecodeError,
    ReaderError,
    TokenError,
)
from emi_reader.keywords import ReaderCaseKeyword, ReaderKeyword
from emi_reader.links import Cta, LinkExtractor, LinkResult, extract_link
from emi_reader.mailbox import Mailbox, RawEmail, ReceivedEmail, WaitOptions
from emi_reader.mime import ParsedEmail, parse_email
from emi_reader.settings import ImapSettings, Security
from emi_reader.tokens import assert_link_for, token_payload, token_subject

__all__ = [
    "Cta",
    "EmailWaitError",
    "ImapConnectionError",
    "ImapSettings",
    "LinkExtractionError",
    "LinkExtractor",
    "LinkResult",
    "Mailbox",
    "MailboxAccessError",
    "MimeDecodeError",
    "ParsedEmail",
    "RawEmail",
    "ReaderCaseKeyword",
    "ReaderError",
    "ReaderKeyword",
    "ReceivedEmail",
    "Security",
    "TokenError",
    "WaitOptions",
    "assert_link_for",
    "extract_link",
    "parse_email",
    "token_payload",
    "token_subject",
]

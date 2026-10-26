# SPDX-FileCopyrightText: 2026 Aleksandr Polskiy
# SPDX-License-Identifier: Apache-2.0
"""Decode a raw email into the parts the reader needs (design §5.6)."""

import binascii
from dataclasses import dataclass, field
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import getaddresses

from emi_reader.errors import MimeDecodeError
from emi_reader.keywords import ReaderKeyword

# Header fields a recipient can be matched on (design §5.4), lower-case.
RECIPIENT_FIELDS: tuple[str, ...] = ("to", "cc", "bcc", "delivered-to", "x-original-to")

_TEST_ID_HEADER = "x-emi-test-id"
_RUN_ID_HEADER = "x-emi-run-id"


@dataclass(frozen=True)
class ParsedEmail:
    """The decoded parts of one email.

    Attributes:
        message_id: The ``Message-ID`` header, or ``""``.
        subject: Decoded subject.
        recipients: Lower-cased addresses per recipient field (keys from ``RECIPIENT_FIELDS``).
        html: Decoded HTML body, or ``None`` when the email has no HTML part.
        text: Decoded plain-text body, or ``None``.
        test_id: The ``X-EMI-Test-Id`` marker header, or ``""``.
        run_id: The ``X-EMI-Run-Id`` marker header, or ``""``.
    """

    message_id: str
    subject: str
    recipients: dict[str, tuple[str, ...]] = field(default_factory=dict)
    html: str | None = None
    text: str | None = None
    test_id: str = ""
    run_id: str = ""

    def addresses_in(self, fields: tuple[str, ...]) -> set[str]:
        """Return every recipient address found in the given fields.

        Args:
            fields: Recipient field names, any case.

        Returns:
            Lower-cased addresses.
        """
        found: set[str] = set()
        for field_name in fields:
            found.update(self.recipients.get(field_name.lower(), ()))
        return found


def _decode_part(part: EmailMessage) -> str:
    """Decode one text part to ``str``.

    Args:
        part: A ``text/*`` MIME part.

    Returns:
        The decoded text.

    Raises:
        MimeDecodeError: If the transfer or character encoding is broken.
    """
    try:
        content = part.get_content()
    except (LookupError, UnicodeDecodeError, ValueError, binascii.Error) as error:
        raise MimeDecodeError(
            f"cannot decode {part.get_content_type()} part: {error}",
            ReaderKeyword.MIME_DECODE_FAILED,
        ) from error
    return content if isinstance(content, str) else content.decode("utf-8", errors="replace")


def _recipients(message: EmailMessage) -> dict[str, tuple[str, ...]]:
    """Collect the addresses of every recipient field.

    Args:
        message: The parsed email.

    Returns:
        Lower-cased addresses per field; fields that are absent are omitted.
    """
    result: dict[str, tuple[str, ...]] = {}
    for field_name in RECIPIENT_FIELDS:
        values = [str(value) for value in message.get_all(field_name, [])]
        if values:
            result[field_name] = tuple(
                address.strip().lower() for _display, address in getaddresses(values) if address
            )
    return result


def parse_email(raw: bytes) -> ParsedEmail:
    """Parse raw RFC 5322 bytes (full message or headers only).

    Args:
        raw: The message as fetched from the server.

    Returns:
        The decoded email. Attachments and inline images are ignored.

    Raises:
        MimeDecodeError: If a text part cannot be decoded.
    """
    message = BytesParser(policy=policy.default).parsebytes(raw)
    html: str | None = None
    text: str | None = None
    for part in message.walk():
        if part.is_multipart() or part.get_content_disposition() == "attachment":
            continue
        content_type = part.get_content_type()
        if content_type == "text/html" and html is None:
            html = _decode_part(part)
        elif content_type == "text/plain" and text is None:
            text = _decode_part(part)
    return ParsedEmail(
        message_id=str(message.get("message-id", "")).strip(),
        subject=str(message.get("subject", "")),
        recipients=_recipients(message),
        html=html,
        text=text,
        test_id=str(message.get(_TEST_ID_HEADER, "")).strip(),
        run_id=str(message.get(_RUN_ID_HEADER, "")).strip(),
    )

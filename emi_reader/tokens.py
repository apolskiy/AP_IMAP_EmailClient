# SPDX-FileCopyrightText: 2026 Aleksandr Polskiy
# SPDX-License-Identifier: Apache-2.0
"""Read the JWT carried by a CTA link and check whom it was issued to (design §5.7)."""

import base64
import binascii
import json
import re
from typing import Any
from urllib.parse import parse_qsl, urlsplit

from emi_reader.errors import TokenError
from emi_reader.keywords import ReaderCaseKeyword

_JWT_PATTERN = re.compile(r"^[A-Za-z0-9_-]+\.([A-Za-z0-9_-]+)\.[A-Za-z0-9_-]+$")


def _b64url_json(segment: str) -> dict[str, Any]:
    """Decode one base64url JWT segment into a JSON object.

    Args:
        segment: The segment without padding.

    Returns:
        The decoded object.

    Raises:
        ValueError: If the segment is not base64url JSON.
    """
    padded = segment + "=" * (-len(segment) % 4)
    try:
        decoded = json.loads(base64.urlsafe_b64decode(padded))
    except (binascii.Error, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(str(error)) from error
    if not isinstance(decoded, dict):
        raise ValueError("payload is not a JSON object")
    return decoded


def token_payload(link: str) -> dict[str, Any]:
    """Return the payload of the first JWT found in the link's query string.

    The signature is not verified: the reader only needs to know whom the token names.

    Args:
        link: Absolute URL.

    Returns:
        The decoded payload.

    Raises:
        TokenError: If the link carries no decodable JWT.
    """
    for _name, value in parse_qsl(urlsplit(link).query):
        match = _JWT_PATTERN.match(value)
        if match:
            try:
                return _b64url_json(match.group(1))
            except ValueError as error:
                raise TokenError(
                    f"token in link is not valid: {error}",
                    ReaderCaseKeyword.LINK_RECIPIENT_MISMATCH,
                    details={"link": link},
                ) from error
    raise TokenError(
        "link carries no token",
        ReaderCaseKeyword.LINK_RECIPIENT_MISMATCH,
        details={"link": link},
    )


def token_subject(link: str) -> str:
    """Return the token's ``sub`` claim (the recipient it was issued to).

    Args:
        link: Absolute URL.

    Returns:
        The subject, as written in the token.

    Raises:
        TokenError: If there is no token or it has no ``sub``.
    """
    subject = token_payload(link).get("sub")
    if not isinstance(subject, str) or not subject:
        raise TokenError(
            "token has no 'sub' claim", ReaderCaseKeyword.LINK_RECIPIENT_MISMATCH, details={"link": link}
        )
    return subject


def assert_link_for(link: str, address: str) -> None:
    """Fail unless the link's token was issued to ``address`` (case-insensitive).

    Args:
        link: Absolute URL.
        address: The test's own address.

    Raises:
        TokenError: With ``CASE_LINK_RECIPIENT_MISMATCH`` if the token names someone else.
    """
    subject = token_subject(link)
    if subject.strip().lower() != address.strip().lower():
        raise TokenError(
            f"link was issued to {subject}, not {address}",
            ReaderCaseKeyword.LINK_RECIPIENT_MISMATCH,
            details={"link": link, "token_sub": subject, "expected": address},
        )

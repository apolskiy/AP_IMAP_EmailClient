# SPDX-FileCopyrightText: 2026 Aleksandr Polskiy
# SPDX-License-Identifier: Apache-2.0
"""Issue tokens with the same structure as the samples (design §6.2).

Tokens are real HS256 JWTs signed with a **test-only** secret: structurally valid, never usable as
real application credentials.
"""

import base64
import hashlib
import hmac
import json
import re
import secrets
from datetime import datetime, timedelta
from typing import Any

TEST_ONLY_SECRET = b"emi-test-only-secret-not-for-production"
_JWT_PATTERN = re.compile(r"eyJ[A-Za-z0-9_-]+\.eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+")
_HEADER = {"alg": "HS256", "typ": "JWT"}
_ACTIVATION_LIFETIME = timedelta(hours=24)


def _b64url(data: bytes) -> str:
    """Base64url without padding."""
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _b64url_json(segment: str) -> dict[str, Any]:
    """Decode a base64url JSON segment."""
    decoded = json.loads(base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4)))
    if not isinstance(decoded, dict):
        raise ValueError("token segment is not a JSON object")
    return decoded


def find_token(text: str) -> str | None:
    """Return the first JWT in a text, or ``None``.

    Args:
        text: Any decoded body text.

    Returns:
        The token string.
    """
    match = _JWT_PATTERN.search(text)
    return match.group(0) if match else None


def token_claims(token: str) -> dict[str, Any]:
    """Return a token's payload (no signature check).

    Args:
        token: A JWT.

    Returns:
        The payload claims.
    """
    return _b64url_json(token.split(".")[1])


def sign(claims: dict[str, Any], secret: bytes = TEST_ONLY_SECRET) -> str:
    """Build an HS256 JWT.

    Args:
        claims: Payload claims.
        secret: Signing key (test-only by default).

    Returns:
        The token.
    """
    head = _b64url(json.dumps(_HEADER, separators=(",", ":")).encode())
    body = _b64url(json.dumps(claims, separators=(",", ":")).encode())
    signature = hmac.new(secret, f"{head}.{body}".encode("ascii"), hashlib.sha256).digest()
    return f"{head}.{body}.{_b64url(signature)}"


def verify(token: str, secret: bytes = TEST_ONLY_SECRET) -> bool:
    """Check a token's HS256 signature.

    Args:
        token: A JWT.
        secret: Signing key.

    Returns:
        True when the signature matches.
    """
    head, body, signature = token.split(".")
    expected = hmac.new(secret, f"{head}.{body}".encode("ascii"), hashlib.sha256).digest()
    return hmac.compare_digest(_b64url(expected), signature)


def reissue(sample_token: str, recipient: str, sent_at: datetime, secret: bytes = TEST_ONLY_SECRET) -> str:
    """Issue a new token shaped like a sample token, for another recipient.

    Every claim of the sample is kept (e.g. ``site``); ``sub`` becomes the recipient, ``exp`` (if
    the sample has one) becomes ``sent_at`` + 24 h, and a random ``jti`` keeps two tokens for the
    same address in the same second distinct.

    Args:
        sample_token: The token found in the sample email.
        recipient: New recipient address.
        sent_at: The email's ``Date``.
        secret: Signing key.

    Returns:
        The new token.
    """
    claims = token_claims(sample_token)
    claims["sub"] = recipient
    if "exp" in claims:
        claims["exp"] = int((sent_at + _ACTIVATION_LIFETIME).timestamp())
    claims["jti"] = secrets.token_hex(8)
    return sign(claims, secret)

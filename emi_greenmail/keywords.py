# SPDX-FileCopyrightText: 2026 Aleksandr Polskiy
# SPDX-License-Identifier: Apache-2.0
"""Failure keywords raised by the Greenmail client (catalog: ``docs/keywords.md``)."""

from enum import StrEnum


class GreenmailKeyword(StrEnum):
    """Keywords for failures of the Greenmail client (``GREENMAIL_`` prefix)."""

    NOT_READY = "GREENMAIL_NOT_READY"
    SMTP_CONNECTION_FAILURE = "GREENMAIL_SMTP_CONNECTION_FAILURE"
    IMAP_CONNECTION_FAILURE = "GREENMAIL_IMAP_CONNECTION_FAILURE"
    API_REQUEST_FAILED = "GREENMAIL_API_REQUEST_FAILED"

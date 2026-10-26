# SPDX-FileCopyrightText: 2026 Aleksandr Polskiy
# SPDX-License-Identifier: Apache-2.0
"""Failure keywords raised by the central API (catalog: ``docs/keywords.md``)."""

from enum import StrEnum


class ApiKeyword(StrEnum):
    """Keywords for failures of the central API (``API_`` prefix)."""

    ADDRESS_INVALID = "API_ADDRESS_INVALID"

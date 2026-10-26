# SPDX-FileCopyrightText: 2026 Aleksandr Polskiy
# SPDX-License-Identifier: Apache-2.0
"""Failure keywords raised by the test broker (catalog: ``docs/keywords.md``)."""

from enum import StrEnum


class TestBrokerKeyword(StrEnum):
    """Keywords for failures of the test broker (``TEST_BROKER_`` prefix)."""

    __test__ = False  # not a pytest test class despite the name

    UNAVAILABLE = "TEST_BROKER_UNAVAILABLE"
    PORT_UNAVAILABLE = "TEST_BROKER_PORT_UNAVAILABLE"
    VERSION_MISMATCH = "TEST_BROKER_VERSION_MISMATCH"
    PURGE_REFUSED = "TEST_BROKER_PURGE_REFUSED"

# SPDX-FileCopyrightText: 2026 Aleksandr Polskiy
# SPDX-License-Identifier: Apache-2.0
"""emi_api: the single entry point tests call (design §8.2)."""

from emi_api.api import (
    EmiApi,
    Health,
    Problem,
    ReaderApi,
    SenderApi,
    register_family,
    registered_families,
    unregister_family,
)
from emi_api.backends import LocalBroker, MailBackend
from emi_api.errors import AddressInvalidError, ApiError, EmiError, keywords_of
from emi_api.identity import EmiRun, RunId, build_address, case_id, marker_for, worker_number
from emi_api.keywords import ApiKeyword
from emi_api.settings import ClientMode, EmiSettings

__all__ = [
    "AddressInvalidError",
    "ApiError",
    "ApiKeyword",
    "ClientMode",
    "EmiApi",
    "EmiError",
    "EmiRun",
    "EmiSettings",
    "Health",
    "LocalBroker",
    "MailBackend",
    "Problem",
    "ReaderApi",
    "RunId",
    "SenderApi",
    "build_address",
    "case_id",
    "keywords_of",
    "marker_for",
    "register_family",
    "registered_families",
    "unregister_family",
    "worker_number",
]

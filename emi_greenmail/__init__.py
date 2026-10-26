# SPDX-FileCopyrightText: 2026 Aleksandr Polskiy
# SPDX-License-Identifier: Apache-2.0
"""Greenmail client: readiness, port probes, configuration, users and purge via the REST API."""

from emi_greenmail.client import GreenmailClient, GreenmailUser, Probe, Readiness
from emi_greenmail.errors import GreenmailApiError, GreenmailError, GreenmailNotReadyError
from emi_greenmail.keywords import GreenmailKeyword
from emi_greenmail.settings import GreenmailSettings

__all__ = [
    "GreenmailApiError",
    "GreenmailClient",
    "GreenmailError",
    "GreenmailKeyword",
    "GreenmailNotReadyError",
    "GreenmailSettings",
    "GreenmailUser",
    "Probe",
    "Readiness",
]

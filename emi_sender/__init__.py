# SPDX-FileCopyrightText: 2026 Aleksandr Polskiy
# SPDX-License-Identifier: Apache-2.0
"""emi_sender: plays the application and delivers realistic test emails to the shared mailbox."""

from emi_sender.batch import BatchPlan, BatchRequest, Order, PlannedEmail, Preset, plan_batch
from emi_sender.compose import (
    ComposedEmail,
    Field,
    Fields,
    Kind,
    Marker,
    Template,
    builtin_templates,
    compose,
    fields_label,
    parse_fields,
    testrail_case_id,
)
from emi_sender.errors import DeliveryError, InvalidInputError, SenderError
from emi_sender.keywords import SenderCaseKeyword, SenderKeyword
from emi_sender.sender import Manifest, ManifestEntry, Sender
from emi_sender.settings import SenderSettings, Security, Transport

__all__ = [
    "BatchPlan",
    "BatchRequest",
    "ComposedEmail",
    "DeliveryError",
    "Field",
    "Fields",
    "InvalidInputError",
    "Kind",
    "Manifest",
    "ManifestEntry",
    "Marker",
    "Order",
    "PlannedEmail",
    "Preset",
    "Security",
    "Sender",
    "SenderCaseKeyword",
    "SenderError",
    "SenderKeyword",
    "SenderSettings",
    "Template",
    "Transport",
    "builtin_templates",
    "compose",
    "fields_label",
    "parse_fields",
    "plan_batch",
    "testrail_case_id",
]

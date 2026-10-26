# SPDX-FileCopyrightText: 2026 Aleksandr Polskiy
# SPDX-License-Identifier: Apache-2.0
"""Settings of the central API: one object for every tool (design §3, §8.2)."""

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from emi_greenmail import GreenmailSettings
from emi_reader import ImapSettings
from emi_sender import SenderSettings
from emi_test_broker import BrokerSettings, settings_from_env


class ClientMode(StrEnum):
    """How the API reaches the sender and reader (design §2)."""

    SERVICE = "service"
    INPROCESS = "inprocess"


@dataclass(frozen=True, kw_only=True)
class EmiSettings:
    """Everything the API needs.

    Attributes:
        mode: ``service`` (through the test broker, default) or ``inprocess``.
        broker: Broker settings; they also carry the IMAP and SMTP settings both modes use.
        greenmail: Greenmail REST API and the mail ports it must accept connections on.
        wait_timeout: Default seconds a wait may take (design §10).
        auto_start: Start the broker once when it does not answer (service mode).
    """

    mode: ClientMode = ClientMode.SERVICE
    broker: BrokerSettings = field(default_factory=BrokerSettings)
    greenmail: GreenmailSettings | None = None
    wait_timeout: float = 60.0
    auto_start: bool = True

    def __post_init__(self) -> None:
        """Validate the wait timeout.

        Raises:
            ValueError: If it is out of range.
        """
        if not 1.0 <= self.wait_timeout <= 3600.0:
            raise ValueError(f"wait_timeout must be 1-3600 s, got {self.wait_timeout}")

    @property
    def imap(self) -> ImapSettings:
        """Reader connection settings."""
        return self.broker.imap

    @property
    def smtp(self) -> SenderSettings:
        """Sender delivery settings."""
        return self.broker.smtp

    @property
    def greenmail_settings(self) -> GreenmailSettings:
        """Greenmail endpoints: explicit ones, or the IMAP host with its SMTP and IMAP ports."""
        if self.greenmail is not None:
            return self.greenmail
        return GreenmailSettings(host=self.imap.host, probe_ports={"smtp": self.smtp.effective_smtp_port,
                                                                   "imap": self.imap.effective_port})

    def describe(self) -> dict[str, Any]:
        """Every setting in effect, passwords redacted.

        Returns:
            A plain dictionary safe to log and report.
        """
        return {"mode": str(self.mode), "wait_timeout": self.wait_timeout, "auto_start": self.auto_start,
                "broker": self.broker.describe(), "greenmail": self.greenmail_settings.describe()}

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None, **overrides: Any) -> "EmiSettings":
        """Build settings from ``EMI_*`` variables (``.env`` already loaded by the caller).

        Args:
            environ: The environment; ``os.environ`` by default.
            **overrides: Explicit values, which win over the environment.

        Returns:
            The settings.

        Raises:
            ValueError: If a value is out of range.
        """
        source = environ if environ is not None else os.environ
        values: dict[str, Any] = {"broker": settings_from_env(source)}
        if source.get("EMI_CLIENT_MODE"):
            values["mode"] = ClientMode(source["EMI_CLIENT_MODE"].strip().lower())
        values.update({key: value for key, value in overrides.items() if value is not None})
        return cls(**values)

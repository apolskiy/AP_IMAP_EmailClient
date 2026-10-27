# SPDX-FileCopyrightText: 2026 Aleksandr Polskiy
# SPDX-License-Identifier: Apache-2.0
"""Test broker settings and its per-start state file (design §7)."""

import hashlib
import json
import os
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, field, fields
from enum import StrEnum
from pathlib import Path
from typing import Any

from emi_config import Option, Resolved, merge, resolve
from emi_reader import ImapSettings
from emi_reader import Security as ImapSecurity
from emi_reader.settings import GREENMAIL_TEST_PASSWORD
from emi_reader.settings import OPTIONS as READER_OPTIONS
from emi_reader.settings import imap_settings_from
from emi_sender import SenderSettings, Transport
from emi_sender import Security as SmtpSecurity
from emi_sender.settings import OPTIONS as SENDER_OPTIONS
from emi_sender.settings import sender_settings_from

BROKER_VERSION = "1.3.0"  # 1.1.0: POST /emails (AMB-74), the wait's "inventory" (AMB-76); 1.2.0: shared
# poller for every wait and the run-scoped purge (AMB-79, AMB-80); 1.3.0: recipient field combinations on
# /send (AMB-82), setting sources on /health (R-CFG-04), expected_folder on /wait (AMB-85)
TOKEN_HEADER = "X-EMI-Broker-Token"


@dataclass(frozen=True, kw_only=True)
class BrokerSettings:  # pylint: disable=too-many-instance-attributes
    """Where the broker listens and which servers it talks to.

    Attributes:
        host: Listen address; loopback only.
        port: Listen port (``EMI_TEST_BROKER_PORT``).
        imap: Reader connection settings.
        smtp: Sender delivery settings.
        max_connections: IMAP/SMTP connections the broker may hold at once, across every run.
        lease_ttl_sec: A run lease expires after this long without a heartbeat.
        idle_shutdown_sec: The broker stops after this long without requests and without leases.
        log_dir: Directory of the dated log files.
        state_dir: Directory of the per-start state file (pid, token, version).
        sources: Where each option's value came from (option name -> ``cli``, ``env``, ``.env``, ``default``
            or ``argument``), shown by ``/health`` (R-CFG-04). Not part of comparisons or the config hash.
    """

    host: str = "127.0.0.1"
    port: int = 8025
    imap: ImapSettings = field(default_factory=ImapSettings)
    smtp: SenderSettings = field(default_factory=SenderSettings)
    max_connections: int = 10
    lease_ttl_sec: float = 300.0
    idle_shutdown_sec: float = 1800.0
    log_dir: Path = Path("logs")
    state_dir: Path = field(default_factory=lambda: Path(tempfile.gettempdir()) / "emi_test_broker")
    sources: dict[str, str] = field(default_factory=dict, compare=False, repr=False)

    def __post_init__(self) -> None:
        """Validate ranges.

        Raises:
            ValueError: If the port, connection budget or a time limit is out of range.
        """
        if not 1 <= self.port <= 65535:
            raise ValueError(f"port must be 1-65535, got {self.port}")
        if not 1 <= self.max_connections <= 100:
            raise ValueError(f"max_connections must be 1-100, got {self.max_connections}")
        for name, value in (("lease_ttl_sec", self.lease_ttl_sec), ("idle_shutdown_sec", self.idle_shutdown_sec)):
            if not 1.0 <= value <= 86400.0:
                raise ValueError(f"{name} must be 1-86400 s, got {value}")

    @property
    def base_url(self) -> str:
        """Root URL of the broker."""
        return f"http://{self.host}:{self.port}"

    @property
    def state_file(self) -> Path:
        """The state file of the broker on this port."""
        return self.state_dir / f"broker_{self.port}.json"

    def describe(self) -> dict[str, Any]:
        """Return the settings for ``/health`` and logs, with passwords redacted.

        Returns:
            A plain dictionary safe to log.
        """
        return {"host": self.host, "port": self.port, "imap": self.imap.describe(), "smtp": self.smtp.describe(),
                "max_connections": self.max_connections, "lease_ttl_sec": self.lease_ttl_sec,
                "idle_shutdown_sec": self.idle_shutdown_sec, "log_dir": str(self.log_dir)}

    @property
    def settings_file(self) -> Path:
        """Where an auto-start writes these settings for the child broker (passwords never included)."""
        return self.state_dir / f"broker_{self.port}_settings.json"

    def config_hash(self) -> str:
        """Fingerprint of everything that changes the broker's behaviour.

        Passwords enter only as their own hash, so a changed password is detected without being exposed.

        Returns:
            16 hex characters.
        """
        material = self.describe()
        material["secrets"] = hashlib.sha256(f"{self.imap.password}\0{self.smtp.password}".encode()).hexdigest()
        return hashlib.sha256(json.dumps(material, sort_keys=True).encode()).hexdigest()[:16]


OPTIONS: tuple[Option, ...] = merge(READER_OPTIONS, SENDER_OPTIONS, (
    Option(name="test_broker_port", env="EMI_TEST_BROKER_PORT", kind="int", default=8025, minimum=1,
           maximum=65535, help="test broker port"),
    Option(name="max_connections", env="EMI_MAX_CONNECTIONS", kind="int", default=10, minimum=1, maximum=100,
           help="IMAP/SMTP connections per host (Gmail allows about 15 per account)"),
    Option(name="log_dir", env="EMI_LOG_DIR", kind="str", default="logs", help="test broker log directory"),
))


def broker_settings_from(resolved: Resolved, **overrides: Any) -> BrokerSettings:
    """The broker settings a resolved table describes.

    Args:
        resolved: Values of a table that includes :data:`OPTIONS`.
        **overrides: Settings no option carries (``state_dir``); None values are ignored.

    Returns:
        The settings.

    Raises:
        ValueError: If a value is out of range.
    """
    values: dict[str, Any] = {"port": resolved["test_broker_port"], "imap": imap_settings_from(resolved),
                              "smtp": sender_settings_from(resolved), "max_connections": resolved["max_connections"],
                              "log_dir": Path(resolved["log_dir"]),
                              "sources": {option.name: str(resolved.sources[option.name]) for option in OPTIONS}}
    values.update({key: value for key, value in overrides.items() if value is not None})
    return BrokerSettings(**values)


def settings_from_env(environ: Mapping[str, str], **overrides: Any) -> BrokerSettings:
    """Build broker settings from ``EMI_*`` variables alone (``.env`` already loaded by the caller).

    Args:
        environ: The environment.
        **overrides: Settings no option carries (``state_dir``).

    Returns:
        The settings.

    Raises:
        ValueError: If a value is out of range or not a number.
    """
    return broker_settings_from(resolve(OPTIONS, environ=environ), **overrides)


def settings_to_json(settings: BrokerSettings) -> dict[str, Any]:
    """Every setting except the passwords, for the auto-started broker's settings file.

    The child must run with exactly the caller's settings (its ``config_hash`` is compared on every start), and
    callers such as the harness have settings that no ``EMI_*`` variable carries.

    Args:
        settings: The settings.

    Returns:
        Plain JSON values.
    """
    def _plain(record: Any) -> dict[str, Any]:
        data = {item.name: getattr(record, item.name) for item in fields(record) if item.name != "password"}
        return {key: (str(value) if isinstance(value, (Path, StrEnum)) else
                      list(value) if isinstance(value, tuple) else value) for key, value in data.items()}

    data = _plain(settings)
    data["imap"], data["smtp"] = _plain(settings.imap), _plain(settings.smtp)
    return data


def settings_from_json(data: Mapping[str, Any], environ: Mapping[str, str]) -> BrokerSettings:
    """Rebuild settings from :func:`settings_to_json`; passwords come from the environment.

    Args:
        data: The settings file's content.
        environ: The child's environment (``EMI_IMAP_PASSWORD`` / ``EMI_SMTP_PASSWORD``).

    Returns:
        The settings.

    Raises:
        ValueError: If a value is out of range.
    """
    imap_data, smtp_data = dict(data["imap"]), dict(data["smtp"])
    imap = ImapSettings(**{**imap_data, "security": ImapSecurity(imap_data["security"]),
                           "folders": tuple(imap_data["folders"]),
                           "password": environ.get("EMI_IMAP_PASSWORD") or GREENMAIL_TEST_PASSWORD})
    smtp = SenderSettings(**{**smtp_data, "security": SmtpSecurity(smtp_data["security"]),
                             "transport": Transport(smtp_data["transport"]),
                             "password": environ.get("EMI_SMTP_PASSWORD") or GREENMAIL_TEST_PASSWORD})
    rest = {key: value for key, value in data.items() if key not in ("imap", "smtp")}
    return BrokerSettings(**{**rest, "imap": imap, "smtp": smtp, "log_dir": Path(rest["log_dir"]),
                             "state_dir": Path(rest["state_dir"])})


@dataclass(frozen=True)
class BrokerState:
    """What a running broker writes for its clients: who it is and the token they must send."""

    pid: int
    port: int
    token: str = field(repr=False)
    version: str = BROKER_VERSION
    config_hash: str = ""

    def save(self, path: Path) -> None:
        """Write the state file, readable by the current user only where the OS allows.

        Args:
            path: Target file.
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"pid": self.pid, "port": self.port, "token": self.token,
                                    "version": self.version, "config_hash": self.config_hash}), encoding="utf-8")
        try:
            os.chmod(path, 0o600)
        except OSError:  # pragma: no cover - best effort on file systems without POSIX modes
            pass

    @classmethod
    def load(cls, path: Path) -> "BrokerState | None":
        """Read a state file.

        Args:
            path: The state file.

        Returns:
            The state, or None when the file is missing or unreadable.
        """
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return cls(pid=int(data["pid"]), port=int(data["port"]), token=str(data["token"]),
                       version=str(data.get("version", "")), config_hash=str(data.get("config_hash", "")))
        except (OSError, ValueError, KeyError, TypeError):
            return None

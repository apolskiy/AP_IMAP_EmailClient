# SPDX-FileCopyrightText: 2026 Aleksandr Polskiy
# SPDX-License-Identifier: Apache-2.0
"""IMAP connection settings and the reader's option rows (design §3, AMB-81)."""

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from emi_config import SECURITY_CHOICES, Option, Resolved

_REDACTED = "***"
# Public password of the local Greenmail test user (README: -Dgreenmail.users=qa:password@example.com).
# Not a secret; any real server's password comes from the environment or .env (AMB-23).
GREENMAIL_TEST_PASSWORD = "password"
_MIN_PORT = 1
_MAX_PORT = 65535
_MIN_TIMEOUT_SEC = 1.0
_MAX_TIMEOUT_SEC = 3600.0


class Security(StrEnum):
    """How the connection is protected."""

    PLAIN = "plain"
    SSL = "ssl"
    STARTTLS = "starttls"


# Greenmail's test ports; real servers use 143 / 993 and are set explicitly.
_DEFAULT_PORTS: dict[Security, int] = {
    Security.PLAIN: 3143,
    Security.SSL: 3993,
    Security.STARTTLS: 3143,
}


@dataclass(frozen=True, kw_only=True)
class ImapSettings:  # pylint: disable=too-many-instance-attributes
    """Everything needed to open and use one IMAP connection.

    The password never appears in ``repr`` or in :meth:`describe`.

    Attributes:
        host: Server host. ``127.0.0.1`` by default: on Windows ``localhost`` may resolve to ``::1``.
        port: Server port; ``None`` picks the default for ``security``.
        security: ``plain``, ``ssl`` or ``starttls``.
        username: Login name of the shared mailbox.
        password: Login password; defaults to the local Greenmail test user's public password.
            Any other password comes from the environment or ``.env`` and is never logged.
        folders: Folders searched, in order.
        connect_timeout: Seconds allowed to open the connection.
        read_timeout: Seconds allowed for one server reply.
        verify_tls: Whether to verify the server certificate.
        ca_file: Optional CA bundle for servers with private certificates.
    """

    host: str = "127.0.0.1"
    port: int | None = None
    security: Security = Security.PLAIN
    username: str = "qa"
    password: str = field(default=GREENMAIL_TEST_PASSWORD, repr=False)
    folders: tuple[str, ...] = ("INBOX",)
    connect_timeout: float = 10.0
    read_timeout: float = 15.0
    verify_tls: bool = True
    ca_file: str | None = None

    def __post_init__(self) -> None:
        """Validate ranges so a bad value fails here, not in the middle of a wait.

        Raises:
            ValueError: If a port, timeout, host or folder list is out of range.
        """
        if not self.host.strip():
            raise ValueError("host must not be empty")
        if self.port is not None and not _MIN_PORT <= self.port <= _MAX_PORT:
            raise ValueError(f"port must be {_MIN_PORT}-{_MAX_PORT}, got {self.port}")
        for name, value in (("connect_timeout", self.connect_timeout), ("read_timeout", self.read_timeout)):
            if not _MIN_TIMEOUT_SEC <= value <= _MAX_TIMEOUT_SEC:
                raise ValueError(f"{name} must be {_MIN_TIMEOUT_SEC}-{_MAX_TIMEOUT_SEC} s, got {value}")
        if not self.folders:
            raise ValueError("at least one folder is required")

    @property
    def effective_port(self) -> int:
        """The port actually used: the explicit one, or the default for the security mode."""
        return self.port if self.port is not None else _DEFAULT_PORTS[self.security]

    @property
    def endpoint(self) -> str:
        """``host:port`` for diagnostics."""
        return f"{self.host}:{self.effective_port}"

    def describe(self) -> dict[str, Any]:
        """Return the settings for logs and reports, with the password redacted.

        Returns:
            A plain dictionary safe to log.
        """
        return {
            "host": self.host,
            "port": self.effective_port,
            "security": str(self.security),
            "username": self.username,
            "password": _REDACTED if self.password else "",
            "folders": list(self.folders),
            "connect_timeout": self.connect_timeout,
            "read_timeout": self.read_timeout,
            "verify_tls": self.verify_tls,
            "ca_file": self.ca_file,
        }


# Rows the sender declares too (APPEND and timeouts) must stay identical to the sender's; merge() checks it.
OPTIONS: tuple[Option, ...] = (
    Option(name="imap_host", env="EMI_IMAP_HOST", kind="str", default="127.0.0.1", help="IMAP server host"),
    Option(name="imap_port", env="EMI_IMAP_PORT", kind="int", default=None, minimum=_MIN_PORT, maximum=_MAX_PORT,
           help="IMAP port (default per security: 3143 plain, 3993 ssl)"),
    Option(name="imap_security", env="EMI_IMAP_SECURITY", kind="choice", default="plain",
           choices=SECURITY_CHOICES, help="IMAP connection security"),
    Option(name="imap_user", env="EMI_IMAP_USER", kind="str", default="qa", help="IMAP login"),
    Option(name="imap_password", env="EMI_IMAP_PASSWORD", kind="str", default=GREENMAIL_TEST_PASSWORD,
           secret=True, help="IMAP password (env or .env only)"),
    Option(name="imap_folders", env="EMI_IMAP_FOLDERS", kind="str", default="INBOX",
           help="comma-separated folders the reader searches"),
    Option(name="connect_timeout", env="EMI_CONNECT_TIMEOUT", kind="float", default=10.0,
           minimum=_MIN_TIMEOUT_SEC, maximum=_MAX_TIMEOUT_SEC, help="seconds to open an IMAP/SMTP connection"),
    Option(name="read_timeout", env="EMI_READ_TIMEOUT", kind="float", default=15.0, minimum=_MIN_TIMEOUT_SEC,
           maximum=_MAX_TIMEOUT_SEC, help="seconds for one IMAP/SMTP reply"),
)


def imap_settings_from(resolved: Resolved) -> ImapSettings:
    """The reader settings a resolved table describes.

    Args:
        resolved: Values of a table that includes :data:`OPTIONS`.

    Returns:
        The settings.

    Raises:
        ValueError: If a value is out of range.
    """
    folders = tuple(item.strip() for item in resolved["imap_folders"].split(",") if item.strip()) or ("INBOX",)
    return ImapSettings(host=resolved["imap_host"], port=resolved["imap_port"],
                        security=Security(resolved["imap_security"]), username=resolved["imap_user"],
                        password=resolved["imap_password"], folders=folders,
                        connect_timeout=resolved["connect_timeout"], read_timeout=resolved["read_timeout"])

# SPDX-FileCopyrightText: 2026 Aleksandr Polskiy
# SPDX-License-Identifier: Apache-2.0
"""Delivery settings for the sender and its option rows (design §3, §6.4, AMB-81)."""

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from emi_config import SECURITY_CHOICES, Option, Resolved

_REDACTED = "***"
# Public password of the local Greenmail test user; any other password comes from the environment.
GREENMAIL_TEST_PASSWORD = "password"


class Security(StrEnum):
    """How the connection is protected."""

    PLAIN = "plain"
    SSL = "ssl"
    STARTTLS = "starttls"


class Transport(StrEnum):
    """How emails reach the shared mailbox."""

    SMTP = "smtp"
    APPEND = "append"


_SMTP_PORTS: dict[Security, int] = {Security.PLAIN: 3025, Security.SSL: 3465, Security.STARTTLS: 3025}
_IMAP_PORTS: dict[Security, int] = {Security.PLAIN: 3143, Security.SSL: 3993, Security.STARTTLS: 3143}


@dataclass(frozen=True, kw_only=True)
class SenderSettings:  # pylint: disable=too-many-instance-attributes
    """Where and how the sender delivers.

    Attributes:
        transport: ``smtp`` (default) or ``append`` (IMAP APPEND straight into the mailbox).
        host: Server host.
        smtp_port: SMTP port; ``None`` picks the default for ``security``.
        imap_port: IMAP port for ``append``; ``None`` picks the default for ``security``.
        security: ``plain``, ``ssl`` or ``starttls``.
        username: Login name; SMTP login is skipped when ``smtp_login`` is False.
        password: Login password; defaults to the local Greenmail test user's public password.
        smtp_login: Whether to authenticate on SMTP (Greenmail does not require it).
        base_mailbox: SMTP envelope recipient: the shared mailbox every email lands in.
        from_address: SMTP envelope sender.
        folder: Folder used by ``append``.
        connect_timeout: Seconds allowed to connect.
        read_timeout: Seconds allowed for one server reply.
        verify_tls: Whether to verify the server certificate.
    """

    transport: Transport = Transport.SMTP
    host: str = "127.0.0.1"
    smtp_port: int | None = None
    imap_port: int | None = None
    security: Security = Security.PLAIN
    username: str = "qa"
    password: str = field(default=GREENMAIL_TEST_PASSWORD, repr=False)
    smtp_login: bool = False
    base_mailbox: str = "qa@example.com"
    from_address: str = "noreply@cloud.example.com"
    folder: str = "INBOX"
    connect_timeout: float = 10.0
    read_timeout: float = 15.0
    verify_tls: bool = True

    def __post_init__(self) -> None:
        """Validate ranges.

        Raises:
            ValueError: If a port or timeout is out of range or an address is empty.
        """
        for name, port in (("smtp_port", self.smtp_port), ("imap_port", self.imap_port)):
            if port is not None and not 1 <= port <= 65535:
                raise ValueError(f"{name} must be 1-65535, got {port}")
        for name, value in (("connect_timeout", self.connect_timeout), ("read_timeout", self.read_timeout)):
            if not 1.0 <= value <= 3600.0:
                raise ValueError(f"{name} must be 1-3600 s, got {value}")
        if "@" not in self.base_mailbox or "@" not in self.from_address:
            raise ValueError("base_mailbox and from_address must be email addresses")

    @property
    def effective_smtp_port(self) -> int:
        """The SMTP port actually used."""
        return self.smtp_port if self.smtp_port is not None else _SMTP_PORTS[self.security]

    @property
    def effective_imap_port(self) -> int:
        """The IMAP port actually used by ``append``."""
        return self.imap_port if self.imap_port is not None else _IMAP_PORTS[self.security]

    @property
    def endpoint(self) -> str:
        """``host:port`` of the active transport, for diagnostics."""
        port = self.effective_smtp_port if self.transport is Transport.SMTP else self.effective_imap_port
        return f"{self.host}:{port}"

    def describe(self) -> dict[str, Any]:
        """Return the settings for logs and reports, with the password redacted.

        Returns:
            A plain dictionary safe to log.
        """
        return {
            "transport": str(self.transport), "host": self.host, "endpoint": self.endpoint,
            "security": str(self.security), "username": self.username,
            "password": _REDACTED if self.password else "", "smtp_login": self.smtp_login,
            "base_mailbox": self.base_mailbox, "from_address": self.from_address, "folder": self.folder,
            "connect_timeout": self.connect_timeout, "read_timeout": self.read_timeout,
            "verify_tls": self.verify_tls,
        }


# imap_port, imap_password and the timeouts are the reader's rows too and must stay identical to them
# (merge() checks it); the sender declares them itself so that it never imports the reader.
OPTIONS: tuple[Option, ...] = (
    Option(name="smtp_host", env="EMI_SMTP_HOST", kind="str", default="127.0.0.1",
           help="mail server host the sender delivers to (SMTP and APPEND)"),
    Option(name="smtp_port", env="EMI_SMTP_PORT", kind="int", default=None, minimum=1, maximum=65535,
           help="SMTP port (default per security: 3025 plain, 3465 ssl)"),
    Option(name="smtp_security", env="EMI_SMTP_SECURITY", kind="choice", default="plain",
           choices=SECURITY_CHOICES, help="SMTP / APPEND connection security"),
    Option(name="smtp_user", env="EMI_SMTP_USER", kind="str", default="qa", help="SMTP / APPEND login"),
    Option(name="smtp_password", env="EMI_SMTP_PASSWORD", kind="str", default=GREENMAIL_TEST_PASSWORD,
           secret=True, help="SMTP password (env or .env only); setting it turns SMTP login on"),
    Option(name="sender_transport", env="EMI_SENDER_TRANSPORT", kind="choice", default="smtp",
           choices=("smtp", "append"), help="how the sender delivers: SMTP or IMAP APPEND"),
    Option(name="base_mailbox", env="EMI_BASE_MAILBOX", kind="str", default="qa@example.com",
           help="the shared mailbox every test address is built on"),
    Option(name="imap_port", env="EMI_IMAP_PORT", kind="int", default=None, minimum=1, maximum=65535,
           help="IMAP port (default per security: 3143 plain, 3993 ssl)"),
    Option(name="imap_password", env="EMI_IMAP_PASSWORD", kind="str", default=GREENMAIL_TEST_PASSWORD,
           secret=True, help="IMAP password (env or .env only)"),
    Option(name="connect_timeout", env="EMI_CONNECT_TIMEOUT", kind="float", default=10.0, minimum=1.0,
           maximum=3600.0, help="seconds to open an IMAP/SMTP connection"),
    Option(name="read_timeout", env="EMI_READ_TIMEOUT", kind="float", default=15.0, minimum=1.0,
           maximum=3600.0, help="seconds for one IMAP/SMTP reply"),
)


def sender_settings_from(resolved: Resolved) -> SenderSettings:
    """The sender settings a resolved table describes.

    ``APPEND`` logs in with the IMAP password, SMTP with the SMTP password; SMTP login is on only when
    the SMTP password was set.

    Args:
        resolved: Values of a table that includes :data:`OPTIONS`.

    Returns:
        The settings.

    Raises:
        ValueError: If a value is out of range.
    """
    transport = Transport(resolved["sender_transport"])
    password = resolved["imap_password"] if transport is Transport.APPEND else resolved["smtp_password"]
    return SenderSettings(transport=transport, host=resolved["smtp_host"], smtp_port=resolved["smtp_port"],
                          imap_port=resolved["imap_port"], security=Security(resolved["smtp_security"]),
                          username=resolved["smtp_user"], password=password,
                          smtp_login=resolved.given("smtp_password"), base_mailbox=resolved["base_mailbox"],
                          connect_timeout=resolved["connect_timeout"], read_timeout=resolved["read_timeout"])

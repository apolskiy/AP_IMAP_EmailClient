# SPDX-FileCopyrightText: 2026 Aleksandr Polskiy
# SPDX-License-Identifier: Apache-2.0
"""Where the Greenmail client finds the server, and its option rows (design §3, §8.1, AMB-81)."""

from dataclasses import dataclass, field
from typing import Any

from emi_config import Option

OPTIONS: tuple[Option, ...] = (
    Option(name="greenmail_host", env="EMI_GREENMAIL_HOST", kind="str", default="",
           help="Greenmail host (default: the IMAP host)"),
    Option(name="greenmail_api_port", env="EMI_GREENMAIL_API_PORT", kind="int", default=8080, minimum=1,
           maximum=65535, help="Greenmail REST API port"),
)


@dataclass(frozen=True, kw_only=True)
class GreenmailSettings:
    """Greenmail endpoints.

    Attributes:
        host: Server host.
        api_port: REST API port.
        probe_ports: Mail ports that must accept connections, by protocol name.
        timeout: Seconds allowed for one HTTP request or one port probe.
    """

    host: str = "127.0.0.1"
    api_port: int = 8080
    probe_ports: dict[str, int] = field(default_factory=lambda: {"smtp": 3025, "imap": 3143})
    timeout: float = 5.0

    def __post_init__(self) -> None:
        """Validate ranges.

        Raises:
            ValueError: If a port or the timeout is out of range.
        """
        for name, port in (("api", self.api_port), *self.probe_ports.items()):
            if not 1 <= port <= 65535:
                raise ValueError(f"{name} port must be 1-65535, got {port}")
        if not 0.1 <= self.timeout <= 300.0:
            raise ValueError(f"timeout must be 0.1-300 s, got {self.timeout}")

    @property
    def base_url(self) -> str:
        """Root URL of the REST API."""
        return f"http://{self.host}:{self.api_port}"

    def describe(self) -> dict[str, Any]:
        """Return the settings for logs and reports.

        Returns:
            A plain dictionary safe to log.
        """
        return {"host": self.host, "api_port": self.api_port, "probe_ports": dict(self.probe_ports),
                "timeout": self.timeout}

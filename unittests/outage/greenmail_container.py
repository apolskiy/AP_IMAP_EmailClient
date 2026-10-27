# SPDX-FileCopyrightText: 2026 Aleksandr Polskiy
# SPDX-License-Identifier: Apache-2.0
"""Stops and starts the Greenmail container for the outage tests (design §13.2, AMB-77)."""

import os
import shutil
import subprocess

from emi_greenmail import GreenmailClient, GreenmailSettings

DEFAULT_CONTAINER = "greenmail_server"
_READY_SEC = 120.0
_DOCKER_SEC = 60.0


class GreenmailContainer:
    """The container named by ``EMI_GREENMAIL_CONTAINER`` (``greenmail_server`` by default)."""

    def __init__(self, name: str | None = None) -> None:
        """Name the container.

        Args:
            name: The container; the environment's ``EMI_GREENMAIL_CONTAINER`` or the default when None.
        """
        self._name = name or os.environ.get("EMI_GREENMAIL_CONTAINER", DEFAULT_CONTAINER)
        self._docker = shutil.which("docker")

    @property
    def name(self) -> str:
        """The container's name."""
        return self._name

    def unavailable_reason(self) -> str:
        """Why the tests cannot run here.

        Returns:
            ``""`` when Docker answers and the container is running, otherwise the reason.
        """
        if self._docker is None:
            return "the docker command is not installed"
        try:
            state = self._run("inspect", "--format", "{{.State.Running}}", self._name)
        except (OSError, subprocess.SubprocessError) as error:
            return f"docker does not answer: {error}"
        if state.returncode != 0:
            return f"no container named {self._name!r} (set EMI_GREENMAIL_CONTAINER)"
        if state.stdout.strip() != "true":
            return f"container {self._name!r} is not running"
        return ""

    def running(self) -> bool:
        """Whether the container is running."""
        return self.unavailable_reason() == ""

    def stop(self) -> None:
        """Stop the container (1 s grace); its ports then refuse connections."""
        self._run("stop", "--time", "1", self._name, check=True)

    def start(self) -> None:
        """Start the container and wait until Greenmail is ready (its mail is gone; its users come back)."""
        self._run("start", self._name, check=True)
        GreenmailClient(GreenmailSettings()).wait_until_ready(_READY_SEC)

    def _run(self, *arguments: str, check: bool = False) -> subprocess.CompletedProcess[str]:
        """Run one docker command."""
        return subprocess.run([str(self._docker), *arguments], capture_output=True, text=True, check=check,
                              timeout=_DOCKER_SEC)

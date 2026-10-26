# SPDX-FileCopyrightText: 2026 Aleksandr Polskiy
# SPDX-License-Identifier: Apache-2.0
"""Greenmail client: readiness, port probes, configuration, users and purge (design §8.1, AMB-16, AMB-42).

The client never starts Greenmail: locally the user starts it (README), in CI the workflow does. When it is
not ready, :meth:`GreenmailClient.check_ready` raises ``GREENMAIL_NOT_READY`` naming every endpoint probed.
"""

import json
import logging
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from emi_greenmail.errors import GreenmailApiError, GreenmailNotReadyError
from emi_greenmail.keywords import GreenmailKeyword
from emi_greenmail.settings import GreenmailSettings

_LOG = logging.getLogger(__name__)
_READY_MESSAGE = "Service running"
_POLL_INTERVAL_SEC = 0.5

# (method, url, JSON body or None, timeout) -> (HTTP status, response body)
HttpCall = Callable[[str, str, bytes | None, float], tuple[int, bytes]]
# (host, port, timeout) -> None when the port accepts a connection; raises OSError otherwise
PortConnector = Callable[[str, int, float], None]


@dataclass(frozen=True)
class GreenmailUser:
    """One configured Greenmail user (mirrors the REST API's ``User``)."""

    login: str
    email: str


@dataclass(frozen=True)
class Probe:
    """Result of one readiness check or port probe."""

    name: str
    endpoint: str
    passed: bool
    detail: str


@dataclass(frozen=True)
class Readiness:
    """Readiness of the server: the REST check plus every port probe."""

    probes: tuple[Probe, ...]

    @property
    def ready(self) -> bool:
        """True when every probe passed."""
        return all(probe.passed for probe in self.probes)

    @property
    def failed(self) -> tuple[Probe, ...]:
        """The probes that did not pass."""
        return tuple(probe for probe in self.probes if not probe.passed)


def urllib_http_call(method: str, url: str, body: bytes | None, timeout: float) -> tuple[int, bytes]:
    """Send one HTTP request with the standard library.

    Args:
        method: HTTP method.
        url: Absolute URL.
        body: JSON body, or None.
        timeout: Seconds allowed.

    Returns:
        The HTTP status and the response body; error statuses are returned, not raised.

    Raises:
        OSError: If the server cannot be reached (refused, timeout, DNS).
    """
    headers = {"Accept": "application/json"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # nosec B310 - http(s) URL built here
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()
    except urllib.error.URLError as error:
        reason = error.reason
        raise reason if isinstance(reason, OSError) else OSError(str(reason)) from error


def socket_connector(host: str, port: int, timeout: float) -> None:
    """Open and close a TCP connection.

    Args:
        host: Server host.
        port: Port to probe.
        timeout: Seconds allowed.

    Raises:
        OSError: If nothing accepts the connection in time.
    """
    with socket.create_connection((host, port), timeout=timeout):
        pass


class GreenmailClient:
    """Talks to one Greenmail server through its REST API."""

    def __init__(
        self,
        settings: GreenmailSettings | None = None,
        *,
        http: HttpCall = urllib_http_call,
        connector: PortConnector = socket_connector,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        """Create the client.

        Args:
            settings: Endpoints; defaults to the local Greenmail.
            http: Sends one HTTP request (a simulated one in unit tests).
            connector: Probes one TCP port (a simulated one in unit tests).
            clock: Monotonic clock for :meth:`wait_until_ready`.
            sleeper: Sleep function for :meth:`wait_until_ready`.
        """
        self._settings = settings or GreenmailSettings()
        self._http = http
        self._connector = connector
        self._clock = clock
        self._sleeper = sleeper

    @property
    def settings(self) -> GreenmailSettings:
        """The endpoints in use."""
        return self._settings

    # --- readiness -------------------------------------------------------------------------------------

    def readiness(self) -> Readiness:
        """Run the REST readiness check and every port probe; never raises.

        Returns:
            Every probe with its outcome.
        """
        probes = [self._probe_api()]
        for name, port in self._settings.probe_ports.items():
            probes.append(self._probe_port(name, port))
        return Readiness(tuple(probes))

    def ready(self) -> bool:
        """Whether Greenmail answers its readiness check and every mail port accepts connections.

        Returns:
            True when ready.
        """
        return self.readiness().ready

    def check_ready(self) -> Readiness:
        """Probe once and raise when anything is not ready.

        Returns:
            The passing readiness result.

        Raises:
            GreenmailNotReadyError: ``GREENMAIL_NOT_READY`` naming every failed endpoint.
        """
        result = self.readiness()
        if not result.ready:
            raise _not_ready(result)
        return result

    def wait_until_ready(self, timeout: float) -> Readiness:
        """Poll until Greenmail is ready (used right after it was started, e.g. in CI).

        Args:
            timeout: Seconds to keep polling.

        Returns:
            The passing readiness result.

        Raises:
            GreenmailNotReadyError: ``GREENMAIL_NOT_READY`` with the last failed probes when time runs out.
        """
        deadline = self._clock() + timeout
        while True:
            result = self.readiness()
            if result.ready:
                return result
            if self._clock() + _POLL_INTERVAL_SEC > deadline:
                raise _not_ready(result, waited=timeout)
            self._sleeper(_POLL_INTERVAL_SEC)

    # --- management ------------------------------------------------------------------------------------

    def configuration(self) -> dict[str, Any]:
        """Return the server configuration (ports, protocols, authentication).

        Returns:
            The configuration as the REST API reports it.

        Raises:
            GreenmailNotReadyError: If the API cannot be reached.
            GreenmailApiError: If the API answers with an error.
        """
        payload = self._request("GET", "/api/configuration")
        if not isinstance(payload, dict):
            raise self._api_error("GET", "/api/configuration", 200, "configuration is not a JSON object")
        return payload

    def users(self) -> tuple[GreenmailUser, ...]:
        """List the configured users.

        Returns:
            Every user with login and email.

        Raises:
            GreenmailNotReadyError: If the API cannot be reached.
            GreenmailApiError: If the API answers with an error.
        """
        payload = self._request("GET", "/api/user")
        if not isinstance(payload, list):
            raise self._api_error("GET", "/api/user", 200, "user list is not a JSON array")
        return tuple(GreenmailUser(login=str(item.get("login", "")), email=str(item.get("email", "")))
                     for item in payload if isinstance(item, dict))

    def create_user(self, email: str, login: str, password: str) -> GreenmailUser:
        """Create a user (for example a second mailbox for multi-account tests).

        Args:
            email: Email address of the user.
            login: Login name.
            password: Password; never logged.

        Returns:
            The created user.

        Raises:
            GreenmailNotReadyError: If the API cannot be reached.
            GreenmailApiError: If the API refuses the user.
        """
        body = json.dumps({"email": email, "login": login, "password": password}).encode("utf-8")
        self._request("POST", "/api/user", body)
        _LOG.info("Greenmail user created: %s (%s)", login, email)
        return GreenmailUser(login=login, email=email)

    def delete_user(self, email_or_login: str) -> None:
        """Delete a user.

        Args:
            email_or_login: The user's email address or login.

        Raises:
            GreenmailNotReadyError: If the API cannot be reached.
            GreenmailApiError: If the user does not exist or the API refuses.
        """
        self._request("DELETE", "/api/user/" + urllib.parse.quote(email_or_login, safe=""))
        _LOG.info("Greenmail user deleted: %s", email_or_login)

    def purge(self) -> None:
        """Delete every stored email of every user.

        Destructive: the shared mailbox keeps earlier runs on purpose, so only the purge tool calls this,
        and the test broker refuses it while runs are active (``TEST_BROKER_PURGE_REFUSED``).

        Raises:
            GreenmailNotReadyError: If the API cannot be reached.
            GreenmailApiError: If the API refuses.
        """
        self._request("POST", "/api/mail/purge")
        _LOG.warning("Greenmail purged: every stored email was deleted")

    # --- helpers ---------------------------------------------------------------------------------------

    def _probe_api(self) -> Probe:
        """Call the readiness endpoint."""
        endpoint = f"{self._settings.host}:{self._settings.api_port}"
        try:
            status, body = self._http("GET", self._settings.base_url + "/api/service/readiness", None,
                                      self._settings.timeout)
        except OSError as error:
            return Probe("api", endpoint, False, f"{type(error).__name__}: {error}")
        message = _json_or_none(body)
        text = message.get("message", "") if isinstance(message, dict) else ""
        if status == 200 and text == _READY_MESSAGE:
            return Probe("api", endpoint, True, text)
        return Probe("api", endpoint, False, f"HTTP {status}: {text or body[:200].decode('utf-8', 'replace')}")

    def _probe_port(self, name: str, port: int) -> Probe:
        """Open one TCP connection."""
        endpoint = f"{self._settings.host}:{port}"
        try:
            self._connector(self._settings.host, port, self._settings.timeout)
        except OSError as error:
            return Probe(name, endpoint, False, f"{type(error).__name__}: {error}")
        return Probe(name, endpoint, True, "accepting connections")

    def _request(self, method: str, path: str, body: bytes | None = None) -> Any:
        """Send one management request and decode the JSON answer."""
        try:
            status, response = self._http(method, self._settings.base_url + path, body, self._settings.timeout)
        except OSError as error:
            probe = Probe("api", f"{self._settings.host}:{self._settings.api_port}", False,
                          f"{type(error).__name__}: {error}")
            raise _not_ready(Readiness((probe,))) from error
        if not 200 <= status < 300:
            payload = _json_or_none(response)
            reason = payload.get("message") if isinstance(payload, dict) else None
            raise self._api_error(method, path, status, str(reason or response[:200].decode("utf-8", "replace")))
        return _json_or_none(response)

    def _api_error(self, method: str, path: str, status: int, reason: str) -> GreenmailApiError:
        """Build ``GREENMAIL_API_REQUEST_FAILED``."""
        return GreenmailApiError(
            f"Greenmail API {method} {path} failed: HTTP {status}: {reason}",
            GreenmailKeyword.API_REQUEST_FAILED,
            details={"method": method, "path": path, "status": status, "reason": reason,
                     "api": self._settings.base_url},
        )


def _json_or_none(body: bytes) -> Any:
    """Decode a JSON body; None when empty or not JSON."""
    try:
        return json.loads(body) if body.strip() else None
    except (ValueError, UnicodeDecodeError):
        return None


def _probe_keyword(probe: Probe) -> str:
    """Keyword of one failed probe: the protocol's own, or ``GREENMAIL_NOT_READY`` for the API."""
    name = probe.name.lower()
    if name.startswith("smtp"):
        return GreenmailKeyword.SMTP_CONNECTION_FAILURE
    if name.startswith("imap"):
        return GreenmailKeyword.IMAP_CONNECTION_FAILURE
    return GreenmailKeyword.NOT_READY


def _keywords(result: Readiness) -> tuple[str, tuple[str, ...]]:
    """Primary and secondary keywords of a failed readiness result.

    API down means the server is down: ``GREENMAIL_NOT_READY`` leads and failed ports are context.
    API up but a mail port closed: that protocol's keyword leads, ``GREENMAIL_NOT_READY`` is context.
    """
    causes = list(dict.fromkeys(_probe_keyword(probe) for probe in result.failed))
    if GreenmailKeyword.NOT_READY in causes or not causes:
        primary = str(GreenmailKeyword.NOT_READY)
    else:
        primary = causes[0]
        causes.append(GreenmailKeyword.NOT_READY)
    return primary, tuple(cause for cause in causes if cause != primary)


def _not_ready(result: Readiness, waited: float | None = None) -> GreenmailNotReadyError:
    """Build the not-ready error naming every failed endpoint."""
    failed = "; ".join(f"{probe.name} {probe.endpoint}: {probe.detail}" for probe in result.failed)
    suffix = f" after waiting {waited:g} s" if waited is not None else ""
    primary, secondary = _keywords(result)
    return GreenmailNotReadyError(
        f"Greenmail is not ready{suffix} ({failed}). Start it as described in README "
        "'Check that it is ready', or check the host and port settings.",
        primary,
        secondary=secondary,
        infrastructure=True,
        details={"failed": [{"name": probe.name, "endpoint": probe.endpoint, "detail": probe.detail}
                            for probe in result.failed]},
    )

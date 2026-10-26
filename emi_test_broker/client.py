# SPDX-FileCopyrightText: 2026 Aleksandr Polskiy
# SPDX-License-Identifier: Apache-2.0
"""Client of the test broker, with one auto-start attempt (design §7).

A test's PRE step calls :meth:`BrokerClient.ensure_running`: it pings ``/health``; if nothing answers it starts
the broker once (binding the port is the lock, so concurrent starts are safe), pings again, and raises
``TEST_BROKER_UNAVAILABLE`` if it is still not ready. A broker of another version or configuration on the
port raises ``TEST_BROKER_VERSION_MISMATCH``; there is no automatic port switching.
"""

import json
import os
import subprocess
import sys
import threading
import time
import urllib.parse
from collections.abc import Callable
from http.client import HTTPConnection, HTTPException
from types import TracebackType
from typing import Any

from emi_reader import Cta, ReceivedEmail
from emi_sender import BatchRequest, Manifest, Marker
from emi_test_broker.codec import (
    batch_request_to_json,
    cta_to_json,
    error_from_json,
    manifest_from_json,
    received_from_json,
)
from emi_test_broker.errors import BrokerUnavailableError
from emi_test_broker.keywords import TestBrokerKeyword
from emi_test_broker.service import SERVICE_NAME
from emi_test_broker.settings import BROKER_VERSION, TOKEN_HEADER, BrokerSettings, BrokerState, settings_to_json

# (method, url, body, headers, timeout) -> (status, body); raises OSError when nothing answers
HttpCall = Callable[[str, str, bytes | None, dict[str, str], float], tuple[int, bytes]]
_HEALTH_TIMEOUT_SEC = 2.0
_CALL_MARGIN_SEC = 30.0
_START_WAIT_SEC = 15.0
_START_POLL_SEC = 0.25


def loopback_http_call(method: str, url: str, body: bytes | None, headers: dict[str, str],
                       timeout: float) -> tuple[int, bytes]:
    """Send one HTTP request straight to the broker.

    ``http.client`` is used instead of ``urllib`` on purpose: ``urllib`` honours ``HTTP_PROXY`` settings,
    which on some CI runners and office machines would send a loopback call to a proxy.

    Args:
        method: HTTP method.
        url: Absolute ``http://host:port/path`` URL.
        body: JSON body, or None.
        headers: Extra headers.
        timeout: Seconds allowed.

    Returns:
        Status and body; error statuses are returned, not raised.

    Raises:
        OSError: If nothing answers (refused, timeout, connection closed).
    """
    parts = urllib.parse.urlsplit(url)
    connection = HTTPConnection(parts.hostname or "127.0.0.1", parts.port or 80, timeout=timeout)
    try:
        connection.request(method, parts.path or "/", body=body,
                           headers={"Content-Type": "application/json", **headers})
        response = connection.getresponse()
        return response.status, response.read()
    except HTTPException as error:
        raise OSError(f"{type(error).__name__}: {error}") from error
    finally:
        connection.close()


START_ERRORS_LOG = "emi_test_broker_start_errors.log"


def spawn_command(settings: BrokerSettings) -> list[str]:
    """The command that starts a broker with these settings (global options before the subcommand).

    Every setting except the passwords reaches the child through the settings file
    (``settings.settings_file``, written by :func:`spawn_broker`), so the child runs with exactly the caller's
    settings and the same ``config_hash``; the passwords go only into the child's environment. Port,
    connection budget, log and state directory are also passed explicitly (the state directory carries the
    token the client must read back).

    Args:
        settings: The settings the broker must run with.

    Returns:
        The argument vector.
    """
    return [sys.executable, "-m", "emi_test_broker", "--port", str(settings.port),
            "--max-connections", str(settings.max_connections), "--log-dir", str(settings.log_dir.resolve()),
            "--state-dir", str(settings.state_dir.resolve()),
            "--settings-file", str(settings.settings_file.resolve()), "serve"]


def spawn_broker(settings: BrokerSettings) -> None:
    """Start the broker detached from the caller.

    The child's standard error goes to ``<log dir>/emi_test_broker_start_errors.log``, so a broker that dies
    before its own log is open still leaves a trace.

    Args:
        settings: The settings the broker must run with.
    """
    settings.log_dir.mkdir(parents=True, exist_ok=True)
    settings.state_dir.mkdir(parents=True, exist_ok=True)
    settings.settings_file.write_text(json.dumps(settings_to_json(settings), indent=2), encoding="utf-8")
    environment = dict(os.environ, EMI_IMAP_PASSWORD=settings.imap.password,
                       EMI_SMTP_PASSWORD=settings.smtp.password)
    options: dict[str, Any] = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "close_fds": True,
                               "env": environment}
    if os.name == "nt":
        # CREATE_NO_WINDOW, not DETACHED_PROCESS: a venv's python.exe is a launcher that starts the real
        # interpreter, and under DETACHED_PROCESS that child opens a new console window. A hidden console is
        # inherited by the child, so the broker runs with no window at all.
        options["creationflags"] = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        options["start_new_session"] = True
    with open(settings.log_dir / START_ERRORS_LOG, "ab") as errors:
        # Detached on purpose: the broker outlives the run that started it.
        subprocess.Popen(spawn_command(settings), stderr=errors, **options)  # pylint: disable=consider-using-with


def identity_problem(health: dict[str, Any], settings: BrokerSettings) -> str:
    """Why a ``/health`` answer is not a broker these settings can use.

    Args:
        health: The answer.
        settings: The caller's settings.

    Returns:
        ``""`` when usable, otherwise a description of what was found.
    """
    if health.get("service") != SERVICE_NAME:
        return "port held by something that is not the test broker"
    found = (health.get("version"), health.get("config_hash"))
    wanted = (BROKER_VERSION, settings.config_hash())
    if found != wanted:
        return f"broker version/config {found[0]}/{found[1]} on the port, this run needs {wanted[0]}/{wanted[1]}"
    return ""


class BrokerClient:
    """Calls the broker; every method raises the same typed errors the library would in-process."""

    def __init__(  # pylint: disable=too-many-arguments
        self,
        settings: BrokerSettings | None = None,
        *,
        http: HttpCall = loopback_http_call,
        spawner: Callable[[BrokerSettings], None] = spawn_broker,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        """Create the client.

        Args:
            settings: Broker settings.
            http: Sends one HTTP request (simulated in unit tests).
            spawner: Starts the broker process (simulated in unit tests).
            clock: Monotonic clock.
            sleeper: Sleep function.
        """
        self._settings = settings or BrokerSettings()
        self._http = http
        self._spawner = spawner
        self._clock = clock
        self._sleeper = sleeper
        self._token: str | None = None

    @property
    def settings(self) -> BrokerSettings:
        """The broker settings."""
        return self._settings

    # --- lifecycle -------------------------------------------------------------------------------------

    def health(self) -> dict[str, Any] | None:
        """Ping ``/health``.

        Returns:
            The answer, or None when nothing answers or the answer is not JSON.
        """
        try:
            status, body = self._http("GET", self._settings.base_url + "/health", None, {}, _HEALTH_TIMEOUT_SEC)
        except OSError:
            return None
        try:
            answer = json.loads(body)
        except ValueError:
            return {"service": "", "status": status}
        return answer if isinstance(answer, dict) else {"service": "", "status": status}

    def ensure_running(self, *, auto_start: bool = True) -> dict[str, Any]:
        """Make sure a usable broker serves the port, starting it once if needed.

        Args:
            auto_start: Start the broker when nothing answers.

        Returns:
            The broker's health.

        Raises:
            BrokerUnavailableError: ``TEST_BROKER_VERSION_MISMATCH``, ``TEST_BROKER_PORT_UNAVAILABLE`` or
                ``TEST_BROKER_UNAVAILABLE``.
        """
        health = self.health()
        if health is None and auto_start:
            self._spawner(self._settings)
            deadline = self._clock() + _START_WAIT_SEC
            while health is None and self._clock() < deadline:
                self._sleeper(_START_POLL_SEC)
                health = self.health()
        if health is None:
            raise BrokerUnavailableError(
                f"Test broker not answering on {self._settings.base_url}"
                + (" after one start attempt" if auto_start else "")
                + f"; see {self._settings.log_dir}/emi_test_broker_<date>.log and {START_ERRORS_LOG}",
                TestBrokerKeyword.UNAVAILABLE, infrastructure=True,
                details={"url": self._settings.base_url, "auto_start": auto_start})
        problem = identity_problem(health, self._settings)
        if problem:
            keyword = (TestBrokerKeyword.VERSION_MISMATCH if health.get("service") == SERVICE_NAME
                       else TestBrokerKeyword.PORT_UNAVAILABLE)
            raise BrokerUnavailableError(
                f"{problem}. Stop it with 'python -m emi_test_broker stop' or set EMI_TEST_BROKER_PORT.",
                keyword, infrastructure=True,
                details={"url": self._settings.base_url, "found_version": health.get("version"),
                         "found_config": health.get("config_hash"), "pid": health.get("pid")})
        return health

    def stop(self) -> bool:
        """Ask the broker to shut down.

        Returns:
            True when a broker answered.
        """
        try:
            self._post("/shutdown", {"requested_by": f"pid {os.getpid()}"}, _HEALTH_TIMEOUT_SEC)
        except BrokerUnavailableError:
            return False
        return True

    # --- sending and waiting ---------------------------------------------------------------------------

    def send(self, kind: str, address: str, count: int = 1, *, marker: Marker | None = None) -> Manifest:
        """Send ``count`` emails of one kind to one address through the broker.

        Args:
            kind: ``activation``, ``invitation`` or ``generic``.
            address: Recipient.
            count: Number of emails.
            marker: Test/run marker.

        Returns:
            The manifest.
        """
        payload = {"kind": kind, "address": address, "count": count, "marker": _marker(marker)}
        return manifest_from_json(self._post("/send", payload, _CALL_MARGIN_SEC + 10.0 + 0.05 * max(count, 0)))

    def send_batch(self, request: BatchRequest, *, marker: Marker | None = None) -> Manifest:
        """Send a batch through the broker.

        Args:
            request: The batch.
            marker: Test/run marker.

        Returns:
            The manifest.
        """
        payload = {"batch": batch_request_to_json(request), "marker": _marker(marker)}
        return manifest_from_json(self._post("/send", payload, _CALL_MARGIN_SEC + 60.0))

    def wait_for_link(  # pylint: disable=too-many-arguments
        self, address: str, cta: Cta, *, timeout: float = 60.0, test_id: str | None = None,
        run_id: str | None = None, match_fields: tuple[str, ...] | None = None,
    ) -> str:
        """Wait for an email with the given button and return its link.

        Args:
            address: The test's own address.
            cta: The button.
            timeout: Seconds to wait (queue time for a connection included).
            test_id: Required test marker.
            run_id: Required run marker.
            match_fields: Recipient fields the address must appear in; ``To`` only by default.

        Returns:
            The CTA link.
        """
        payload = {"address": address, "cta": cta_to_json(cta), "mode": "link", "timeout": timeout,
                   "test_id": test_id, "run_id": run_id, "match_fields": _fields(match_fields)}
        return str(self._post("/wait", payload, timeout + _CALL_MARGIN_SEC)["link"])

    def wait_for_emails(  # pylint: disable=too-many-arguments
        self, address: str, *, cta: Cta | None = None, count: int = 1, timeout: float = 60.0,
        test_id: str | None = None, run_id: str | None = None, match_fields: tuple[str, ...] | None = None,
    ) -> list[ReceivedEmail]:
        """Wait until exactly ``count`` matching emails are present.

        Args:
            address: The test's own address.
            cta: Only emails with this button count; None accepts any.
            count: Number expected.
            timeout: Seconds to wait.
            test_id: Required test marker.
            run_id: Required run marker.
            match_fields: Recipient fields the address must appear in; ``To`` only by default.

        Returns:
            The emails, oldest first.
        """
        payload = {"address": address, "cta": cta_to_json(cta), "mode": "emails", "count": count,
                   "timeout": timeout, "test_id": test_id, "run_id": run_id,
                   "match_fields": _fields(match_fields)}
        answer = self._post("/wait", payload, timeout + _CALL_MARGIN_SEC)
        return [received_from_json(item) for item in answer["emails"]]

    # --- leases and maintenance ------------------------------------------------------------------------

    def acquire_lease(self, run_id: str, holder: str) -> None:
        """Register a run.

        Args:
            run_id: The run's ID.
            holder: Who runs it (host, pid, mode).
        """
        self._post("/lease", {"run_id": run_id, "holder": holder}, _HEALTH_TIMEOUT_SEC * 5)

    def heartbeat(self, run_id: str) -> bool:
        """Renew a lease.

        Args:
            run_id: The run's ID.

        Returns:
            False when the lease had expired (the caller re-acquires).
        """
        return bool(self._post("/lease/heartbeat", {"run_id": run_id}, _HEALTH_TIMEOUT_SEC * 5)["renewed"])

    def release_lease(self, run_id: str) -> None:
        """End a run's lease.

        Args:
            run_id: The run's ID.
        """
        self._post("/lease/release", {"run_id": run_id}, _HEALTH_TIMEOUT_SEC * 5)

    def purge(self, *, dry_run: bool = False, older_than_days: int | None = None,
              everything: bool = False) -> dict[str, Any]:
        """Purge the shared mailbox (refused while runs are active).

        Args:
            dry_run: Count only.
            older_than_days: Only mail older than this many days.
            everything: Every email, not only EMI-marked mail.

        Returns:
            Counts per folder.
        """
        payload = {"dry_run": dry_run, "older_than_days": older_than_days, "everything": everything}
        return self._post("/purge", payload, _CALL_MARGIN_SEC * 4)

    # --- helpers ---------------------------------------------------------------------------------------

    def _post(self, path: str, payload: dict[str, Any], timeout: float) -> dict[str, Any]:
        """Send one authenticated request; rebuild typed errors from the answer."""
        body = json.dumps(payload).encode("utf-8")
        for attempt in range(2):
            if self._token is None or attempt:
                state = BrokerState.load(self._settings.state_file)
                self._token = state.token if state else ""
            try:
                status, answer = self._http("POST", self._settings.base_url + path, body,
                                            {TOKEN_HEADER: self._token}, timeout)
            except OSError as error:
                raise BrokerUnavailableError(
                    f"Test broker not answering on {self._settings.base_url}{path}: {error}",
                    TestBrokerKeyword.UNAVAILABLE, infrastructure=True,
                    details={"url": self._settings.base_url + path, "error": str(error)}) from error
            if status != 401:
                break
        decoded = _decode(answer)
        if status == 200:
            return decoded
        error = decoded.get("error") if isinstance(decoded.get("error"), dict) else {}
        if status == 422:
            raise error_from_json(error)
        raise BrokerUnavailableError(
            f"Test broker answered {status} on {path}: {error.get('message', '')}",
            TestBrokerKeyword.UNAVAILABLE, infrastructure=True,
            details={"url": self._settings.base_url + path, "status": status, "error": error})


class LeaseKeeper:
    """Holds a run lease for a ``with`` block: acquire, heartbeat every 30 s, release."""

    def __init__(self, client: BrokerClient, run_id: str, holder: str, interval_sec: float = 30.0) -> None:
        """Prepare the lease.

        Args:
            client: Broker client.
            run_id: The run's ID.
            holder: Who runs it.
            interval_sec: Heartbeat interval.
        """
        self._client = client
        self._run_id = run_id
        self._holder = holder
        self._interval = interval_sec
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._beat, daemon=True, name=f"lease-{run_id}")

    def __enter__(self) -> "LeaseKeeper":
        """Acquire and start heartbeating."""
        self._client.acquire_lease(self._run_id, self._holder)
        self._thread.start()
        return self

    def __exit__(self, exc_type: type[BaseException] | None, exc: BaseException | None,
                 traceback: TracebackType | None) -> None:
        """Stop heartbeating and release."""
        self._stop.set()
        self._thread.join(timeout=5.0)
        try:
            self._client.release_lease(self._run_id)
        except BrokerUnavailableError:
            pass  # the lease expires on its own after the TTL

    def _beat(self) -> None:
        """Renew until stopped; re-acquire after an expiry; survive a broker restart."""
        while not self._stop.wait(self._interval):
            try:
                if not self._client.heartbeat(self._run_id):
                    self._client.acquire_lease(self._run_id, self._holder)
            except BrokerUnavailableError:
                continue


def _marker(marker: Marker | None) -> dict[str, str]:
    """Marker as JSON."""
    return {"test_id": marker.test_id, "run_id": marker.run_id} if marker else {}


def _decode(answer: bytes) -> dict[str, Any]:
    """Decode a JSON object answer; {} otherwise."""
    try:
        decoded = json.loads(answer)
    except ValueError:
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _fields(match_fields: tuple[str, ...] | None) -> list[str] | None:
    """Recipient fields as JSON (None keeps the reader's default)."""
    return list(match_fields) if match_fields is not None else None

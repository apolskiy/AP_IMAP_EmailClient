# SPDX-FileCopyrightText: 2026 Aleksandr Polskiy
# SPDX-License-Identifier: Apache-2.0
"""``EmiApi``: the single entry point tests call (design §8.2).

```python
api = EmiApi(settings)                    # picks service / inprocess mode
run = api.start_run("400001")             # -> EmiRun; registers the run lease (service mode)
api.health().ready                        # Greenmail + broker, never raises
manifest = api.sender.send("activation", address=run.address(), count=1)
link = api.reader.wait_for_link(run.address(), Cta.ACTIVATE_ACCOUNT)
api.greenmail.ready()
```
Synchronous and blocking. Every error a test sees is typed (:data:`emi_api.errors.EmiError`) and carries its
primary and secondary keywords; ``service`` and ``inprocess`` mode raise the same classes.
"""

import json
import os
import threading
from collections.abc import Callable, Mapping
from contextlib import ExitStack
from dataclasses import dataclass, field
from types import TracebackType
from typing import Any

from emi_api.backends import LocalBroker, MailBackend, holder_name
from emi_api.errors import keywords_of
from emi_api.identity import EmiRun, RunId, marker_for, worker_number
from emi_api.settings import ClientMode, EmiSettings
from emi_greenmail import GreenmailClient, GreenmailError
from emi_reader import Cta, ReceivedEmail
from emi_sender import BatchRequest, Manifest, Marker
from emi_test_broker import BrokerClient, BrokerService, LeaseKeeper, TestBrokerError

FamilyFactory = Callable[["EmiApi"], Any]
Observer = Callable[[str, Any], None]
_FAMILIES: dict[str, FamilyFactory] = {}


class _Observers:
    """Callbacks told about evidence (a sender manifest, ...) as calls finish; the harness attaches it."""

    def __init__(self) -> None:
        """No observers yet."""
        self._items: list[Observer] = []
        self._lock = threading.Lock()

    def add(self, observer: Observer) -> Callable[[], None]:
        """Register an observer; return the call that removes it."""
        with self._lock:
            self._items.append(observer)

        def _remove() -> None:
            with self._lock:
                if observer in self._items:
                    self._items.remove(observer)
        return _remove

    def notify(self, name: str, data: Any) -> None:
        """Tell every observer."""
        with self._lock:
            observers = list(self._items)
        for observer in observers:
            observer(name, data)


def register_family(name: str) -> Callable[[FamilyFactory], FamilyFactory]:
    """Register a test family's helper, built once per API on first use (design §8.2 plugin registry).

    Args:
        name: Family name, e.g. ``"invitation"``.

    Returns:
        A decorator for a factory ``(api) -> helper``.

    Raises:
        ValueError: If the name is already registered.
    """
    def _register(factory: FamilyFactory) -> FamilyFactory:
        if name in _FAMILIES:
            raise ValueError(f"test family '{name}' is already registered")
        _FAMILIES[name] = factory
        return factory
    return _register


def registered_families() -> tuple[str, ...]:
    """Names of every registered family, sorted.

    Returns:
        The names.
    """
    return tuple(sorted(_FAMILIES))


def unregister_family(name: str) -> None:
    """Remove a family (tests and reloads).

    Args:
        name: Family name; unknown names are ignored.
    """
    _FAMILIES.pop(name, None)


@dataclass(frozen=True)
class Problem:
    """One thing that is not ready.

    Attributes:
        component: ``greenmail`` or ``broker``.
        primary: Primary keyword.
        secondary: Secondary keywords.
        message: What failed, naming the endpoint.
    """

    component: str
    primary: str
    secondary: tuple[str, ...]
    message: str


@dataclass(frozen=True)
class Health:
    """Readiness of everything a test needs (design §8.2, PRE steps §9.6).

    Attributes:
        mode: ``service`` or ``inprocess``.
        problems: What is not ready; empty when ready.
        broker: The broker's (or in-process service's) health document, when it answered.
        settings: Every setting in effect, passwords redacted.
    """

    mode: str
    problems: tuple[Problem, ...] = ()
    broker: dict[str, Any] | None = None
    settings: dict[str, Any] = field(default_factory=dict)

    @property
    def ready(self) -> bool:
        """True when nothing failed."""
        return not self.problems

    @property
    def keywords(self) -> tuple[str, tuple[str, ...]]:
        """Primary keyword of the first problem, and every other keyword as secondary."""
        if not self.problems:
            return "", ()
        first, rest = self.problems[0], self.problems[1:]
        secondary = list(first.secondary)
        for problem in rest:
            secondary.extend((problem.primary, *problem.secondary))
        return first.primary, tuple(dict.fromkeys(item for item in secondary if item != first.primary))


class SenderApi:
    """``api.sender``: plays the application (design §6)."""

    def __init__(self, backend: MailBackend, observers: _Observers | None = None) -> None:
        """Wrap the backend.

        Args:
            backend: Service or in-process backend.
            observers: Told about every manifest (``"manifest"``, its JSON).
        """
        self._backend = backend
        self._observers = observers or _Observers()

    def send(self, kind: str, address: str, count: int = 1, *, marker: Marker | None = None) -> Manifest:
        """Send ``count`` emails of one kind to one address.

        Args:
            kind: ``activation``, ``invitation`` or ``generic``.
            address: Recipient; a test address supplies the marker when none is given.
            count: Number of emails.
            marker: Test/run marker.

        Returns:
            The manifest.
        """
        return self._observed(self._backend.send(kind, address, count, marker=marker or marker_for(address)))

    def send_batch(self, request: BatchRequest, *, marker: Marker | None = None) -> Manifest:
        """Send a batch (presets, orders, field mixes, design §6.2).

        Args:
            request: The batch.
            marker: Test/run marker; defaults to the first address's.

        Returns:
            The manifest.
        """
        default = marker_for(request.addresses[0]) if request.addresses else None
        return self._observed(self._backend.send_batch(request, marker=marker or default))

    def _observed(self, manifest: Manifest) -> Manifest:
        """Hand the manifest to the observers (the harness attaches it to the test's report)."""
        self._observers.notify("manifest", json.loads(manifest.to_json()))
        return manifest


class ReaderApi:
    """``api.reader``: waits for a test's email (design §5)."""

    def __init__(self, backend: MailBackend, default_timeout: float) -> None:
        """Wrap the backend.

        Args:
            backend: Service or in-process backend.
            default_timeout: Seconds a wait takes when the caller gives none.
        """
        self._backend = backend
        self._default_timeout = default_timeout

    def wait_for_link(  # pylint: disable=too-many-arguments
        self, address: str, cta: Cta, timeout: float | None = None, *, test_id: str | None = None,
        run_id: str | None = None, match_fields: tuple[str, ...] | None = None,
    ) -> str:
        """Wait for an email with the given button and return its link.

        Args:
            address: The test's own address (exact match is the isolation; markers are optional).
            cta: The button.
            timeout: Seconds to wait; the settings' ``wait_timeout`` by default.
            test_id: Also require this test marker.
            run_id: Also require this run marker.
            match_fields: Recipient fields the address must appear in (``to``, ``cc``, ``bcc``,
                ``delivered-to``, ``x-original-to``); ``To`` only by default.

        Returns:
            The CTA link.
        """
        return self._backend.wait_for_link(address, cta, timeout=timeout or self._default_timeout,
                                           test_id=test_id, run_id=run_id, match_fields=match_fields)

    def wait_for_emails(  # pylint: disable=too-many-arguments
        self, address: str, cta: Cta | None = None, count: int = 1, timeout: float | None = None, *,
        test_id: str | None = None, run_id: str | None = None, match_fields: tuple[str, ...] | None = None,
    ) -> list[ReceivedEmail]:
        """Wait until exactly ``count`` matching emails are present.

        Args:
            address: The test's own address.
            cta: Only emails with this button count; None accepts any.
            count: Number expected.
            timeout: Seconds to wait; the settings' ``wait_timeout`` by default.
            test_id: Also require this test marker.
            run_id: Also require this run marker.
            match_fields: Recipient fields the address must appear in; ``To`` only by default.

        Returns:
            The emails, oldest first.
        """
        return self._backend.wait_for_emails(address, cta=cta, count=count,
                                             timeout=timeout or self._default_timeout, test_id=test_id,
                                             run_id=run_id, match_fields=match_fields)


class EmiApi:  # pylint: disable=too-many-instance-attributes
    """The central API. Use as a context manager, or call :meth:`close`."""

    def __init__(  # pylint: disable=too-many-arguments
        self,
        settings: EmiSettings | None = None,
        *,
        run_id: str | None = None,
        environ: Mapping[str, str] | None = None,
        backend: MailBackend | None = None,
        greenmail: GreenmailClient | None = None,
        lease_interval_sec: float = 30.0,
    ) -> None:
        """Create the API.

        Args:
            settings: Settings; ``EmiSettings.from_env()`` by default.
            run_id: This worker's run ID; otherwise ``EMI_RUN_ID`` (made this worker's), otherwise a new one.
            environ: The environment for settings, ``EMI_RUN_ID`` and the worker number.
            backend: Replaces the mode's backend (simulations in unit tests).
            greenmail: Replaces the Greenmail client (simulations in unit tests).
            lease_interval_sec: Lease heartbeat interval (service mode).
        """
        self._settings = settings or EmiSettings.from_env(environ)
        self._run_id = run_id or _run_id_from(environ)
        self._backend = backend or _default_backend(self._settings)
        self._greenmail = greenmail or GreenmailClient(self._settings.greenmail_settings)
        self._lease_interval = lease_interval_sec
        self._lease: ExitStack | None = None
        self._lock = threading.Lock()
        self._families: dict[str, Any] = {}
        self._observers = _Observers()
        self.sender = SenderApi(self._backend, self._observers)
        self.reader = ReaderApi(self._backend, self._settings.wait_timeout)

    # --- identity --------------------------------------------------------------------------------------

    @property
    def settings(self) -> EmiSettings:
        """The settings in effect."""
        return self._settings

    @property
    def mode(self) -> ClientMode:
        """``service`` or ``inprocess``."""
        return self._settings.mode

    @property
    def run_id(self) -> str:
        """This worker's run ID."""
        return self._run_id

    @property
    def greenmail(self) -> GreenmailClient:
        """``api.greenmail``: readiness, users and configuration (design §8.1)."""
        return self._greenmail

    def start_run(self, test_id: str) -> EmiRun:
        """Start one test: its identity and address; the run lease is registered once per worker.

        Args:
            test_id: The test's ID, e.g. ``400001``.

        Returns:
            The test's run.

        Raises:
            AddressInvalidError: ``API_ADDRESS_INVALID``.
            BrokerUnavailableError: ``TEST_BROKER_UNAVAILABLE`` (service mode, broker not reachable).
        """
        run = EmiRun(test_id, self._run_id, self._settings.smtp.base_mailbox)
        self._ensure_lease()
        return run

    def observe(self, observer: Observer) -> Callable[[], None]:
        """Be told about evidence as calls finish: ``observer("manifest", <manifest JSON>)`` after each send.

        Args:
            observer: Called with a name and JSON-ready data; must not raise.

        Returns:
            A call that removes the observer.
        """
        return self._observers.add(observer)

    def family(self, name: str) -> Any:
        """The helper of a registered test family, built once.

        Args:
            name: Family name.

        Returns:
            The helper.

        Raises:
            KeyError: If no such family is registered.
        """
        with self._lock:
            if name not in self._families:
                if name not in _FAMILIES:
                    raise KeyError(f"test family '{name}' is not registered; known: {', '.join(registered_families())}")
                self._families[name] = _FAMILIES[name](self)
            return self._families[name]

    # --- readiness -------------------------------------------------------------------------------------

    def health(self) -> Health:
        """Greenmail readiness and broker health; never raises (PRE steps verify ``.ready``).

        Returns:
            The health, with each problem's keywords.
        """
        problems: list[Problem] = []
        try:
            self._greenmail.check_ready()
        except GreenmailError as error:
            problems.append(_problem("greenmail", error))
        broker: dict[str, Any] | None = None
        try:
            broker = self._broker_health()
        except TestBrokerError as error:
            problems.append(_problem("broker", error))
        return Health(mode=str(self.mode), problems=tuple(problems), broker=broker,
                      settings=self._settings.describe())

    # --- lifecycle -------------------------------------------------------------------------------------

    def close(self) -> None:
        """Release the run lease and close in-process connections."""
        with self._lock:
            lease, self._lease = self._lease, None
        if lease is not None:
            lease.close()
        if isinstance(self._backend, LocalBroker):
            self._backend.close()

    def __enter__(self) -> "EmiApi":
        """Enter the ``with`` block."""
        return self

    def __exit__(self, exc_type: type[BaseException] | None, exc: BaseException | None,
                 traceback: TracebackType | None) -> None:
        """Close on leaving the block."""
        self.close()

    # --- helpers ---------------------------------------------------------------------------------------

    def _broker_health(self) -> dict[str, Any]:
        """Health of the backend (starts the broker once in service mode)."""
        if isinstance(self._backend, BrokerClient):
            return self._backend.ensure_running(auto_start=self._settings.auto_start)
        health = getattr(self._backend, "health", None)
        return dict(health()) if callable(health) else {}

    def _ensure_lease(self) -> None:
        """Register this worker's run with the broker once (service mode only)."""
        if not isinstance(self._backend, BrokerClient):
            return
        with self._lock:
            if self._lease is not None:
                return
            self._backend.ensure_running(auto_start=self._settings.auto_start)
            stack = ExitStack()
            stack.enter_context(LeaseKeeper(self._backend, self._run_id, holder_name(str(self.mode)),
                                            self._lease_interval))
            self._lease = stack


def _run_id_from(environ: Mapping[str, str] | None) -> str:
    """``EMI_RUN_ID`` made this worker's, or a new run ID."""
    source = environ if environ is not None else os.environ
    worker = worker_number(source)
    shared = source.get("EMI_RUN_ID", "").strip()
    run = RunId.parse(shared).for_worker(worker) if shared else RunId.new(worker)
    return str(run)


def _default_backend(settings: EmiSettings) -> MailBackend:
    """The mode's backend."""
    if settings.mode is ClientMode.INPROCESS:
        return LocalBroker(BrokerService(settings.broker))
    return BrokerClient(settings.broker)


def _problem(component: str, error: Exception) -> Problem:
    """A problem from a typed error."""
    primary, secondary = keywords_of(error)
    return Problem(component, primary, secondary, str(error))

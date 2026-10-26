# SPDX-FileCopyrightText: 2026 Aleksandr Polskiy
# SPDX-License-Identifier: Apache-2.0
"""Send a planned batch and report every email in a manifest (design §6.3, §6.6)."""

import json
import logging
import random
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from emi_sender.batch import BatchPlan, BatchRequest, PlannedEmail, plan_batch
from emi_sender.compose import Kind, Marker, Template, builtin_templates, compose
from emi_sender.delivery import UNREACHABLE_KEYWORDS, Delivery, deliver_with_retry, make_delivery
from emi_sender.errors import DeliveryError
from emi_sender.keywords import SenderKeyword
from emi_sender.settings import SenderSettings
from emi_sender.tokens import TEST_ONLY_SECRET

_LOGGER = logging.getLogger(__name__)
SEND_BUDGET_SEC = 10.0
BATCH_BASE_SEC = 10.0
BATCH_PER_EMAIL_SEC = 0.05

EXIT_OK = 0
EXIT_PARTIAL = 1
EXIT_INVALID = 2
EXIT_UNREACHABLE = 3


@dataclass(frozen=True)
class ManifestEntry:  # pylint: disable=too-many-instance-attributes
    """What happened to one email.

    Attributes:
        index: Position in the send order.
        address: Recipient.
        field: Recipient field.
        kind: Email kind.
        status: ``sent`` or ``failed``.
        link: CTA link (``None`` for generic).
        jti: Token ID (``None`` for generic).
        message_id: ``Message-ID`` header.
        keyword: Failure keyword, empty when sent.
        error: Failure message, empty when sent.
        attempts: Delivery attempts used.
        duration_ms: Time spent on this email.
    """

    index: int
    address: str
    field: str
    kind: str
    status: str
    link: str | None = None
    jti: str | None = None
    message_id: str = ""
    keyword: str = ""
    error: str = ""
    attempts: int = 0
    duration_ms: int = 0


@dataclass
class Manifest:
    """The result of a batch; returned by the API, printed by the CLI, saved in the run directory.

    Attributes:
        entries: One entry per planned email, in send order.
        seed: Seed that reproduces the plan.
        test_id: Marker test ID.
        run_id: Marker run ID.
        settings: Delivery settings (password redacted).
        clamped: Count fields that were negative and sent nothing.
    """

    entries: list[ManifestEntry] = field(default_factory=list)
    seed: int = 0
    test_id: str = ""
    run_id: str = ""
    settings: dict[str, Any] = field(default_factory=dict)
    clamped: tuple[str, ...] = ()

    def __len__(self) -> int:
        """Number of planned emails."""
        return len(self.entries)

    def __getitem__(self, index: int) -> ManifestEntry:
        """Entry by position."""
        return self.entries[index]

    @property
    def sent(self) -> list[ManifestEntry]:
        """Entries delivered successfully."""
        return [entry for entry in self.entries if entry.status == "sent"]

    @property
    def failed(self) -> list[ManifestEntry]:
        """Entries that could not be delivered."""
        return [entry for entry in self.entries if entry.status != "sent"]

    @property
    def exit_code(self) -> int:
        """CLI exit code: 0 all sent, 3 server unreachable, 1 other failures."""
        if not self.failed:
            return EXIT_OK
        if any(entry.keyword in UNREACHABLE_KEYWORDS for entry in self.failed):
            return EXIT_UNREACHABLE
        return EXIT_PARTIAL

    @property
    def keywords(self) -> tuple[str, tuple[str, ...]]:
        """Primary keyword and secondary keywords (one per distinct failure cause)."""
        causes = tuple(dict.fromkeys(entry.keyword for entry in self.failed))
        if not causes:
            return "", ()
        if self.exit_code == EXIT_UNREACHABLE:
            primary = next(cause for cause in causes if cause in UNREACHABLE_KEYWORDS)
            return primary, tuple(cause for cause in causes if cause != primary)
        return SenderKeyword.PARTIAL_FAILURE, causes

    def to_json(self) -> str:
        """Serialize for printing and saving."""
        primary, secondary = self.keywords
        data = {"seed": self.seed, "test_id": self.test_id, "run_id": self.run_id,
                "exit_code": self.exit_code, "primary_keyword": primary, "secondary_keywords": list(secondary),
                "clamped": list(self.clamped), "settings": self.settings,
                "entries": [asdict(entry) for entry in self.entries]}
        return json.dumps(data, indent=2)

    def save(self, path: Path) -> None:
        """Write the manifest as JSON.

        Args:
            path: Target file; parent directories are created.
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.to_json(), encoding="utf-8")


class Sender:
    """Compose and deliver batches; each worker thread uses its own connection."""

    def __init__(  # pylint: disable=too-many-arguments
        self,
        settings: SenderSettings | None = None,
        *,
        templates: dict[Kind, Template] | None = None,
        secret: bytes = TEST_ONLY_SECRET,
        delivery_factory: Callable[[SenderSettings], Delivery] = make_delivery,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        """Create a sender.

        Args:
            settings: Delivery settings (defaults: local Greenmail by SMTP).
            templates: Templates per kind (defaults: the built-in ones).
            secret: Token signing key (test-only by default).
            delivery_factory: Builds one delivery per worker (simulations in unit tests).
            clock: Monotonic clock.
            sleeper: Sleep function.
        """
        self._settings = settings or SenderSettings()
        self._templates = templates or builtin_templates()
        self._secret = secret
        self._delivery_factory = delivery_factory
        self._clock = clock
        self._sleeper = sleeper

    def send(self, kind: str, address: str, count: int = 1, *, marker: Marker | None = None) -> Manifest:
        """Send ``count`` emails of one kind to one address (the common test case).

        Args:
            kind: ``activation``, ``invitation`` or ``generic``.
            address: Recipient address.
            count: Number of emails; negative sends nothing (logged).
            marker: Test/run marker.

        Returns:
            The manifest.
        """
        return self.send_batch(BatchRequest(addresses=(address,), counts={kind: count}), marker=marker)

    def send_batch(self, request: BatchRequest, *, marker: Marker | None = None) -> Manifest:
        """Validate, plan and deliver a batch.

        Args:
            request: The batch request.
            marker: Test/run marker for every email.

        Returns:
            The manifest; failures are recorded per email, not raised.

        Raises:
            InvalidInputError: If the request is invalid.
        """
        marker = marker or Marker()
        plan = plan_batch(request)
        manifest = Manifest(seed=plan.seed, test_id=marker.test_id, run_id=marker.run_id,
                            settings=self._settings.describe(), clamped=plan.clamped)
        deadline = self._clock() + BATCH_BASE_SEC + BATCH_PER_EMAIL_SEC * len(plan.emails)
        lanes = [plan.emails[lane::plan.concurrency] for lane in range(plan.concurrency)]
        results: dict[int, ManifestEntry] = {}
        with ThreadPoolExecutor(max_workers=plan.concurrency) as pool:
            for future in [pool.submit(self._run_lane, lane, plan, marker, deadline) for lane in lanes if lane]:
                results.update((entry.index, entry) for entry in future.result())
        manifest.entries = [results[index] for index in sorted(results)]
        primary, secondary = manifest.keywords
        _LOGGER.info("batch done: %d sent, %d failed, seed=%d %s %s", len(manifest.sent),
                     len(manifest.failed), plan.seed, primary, ",".join(secondary))
        return manifest

    def _run_lane(
        self, emails: tuple[PlannedEmail, ...], plan: BatchPlan, marker: Marker, deadline: float
    ) -> list[ManifestEntry]:
        """Deliver one lane of the batch sequentially over one connection."""
        delivery = self._delivery_factory(self._settings)
        rng = random.Random(plan.seed + (emails[0].index if emails else 0))
        entries: list[ManifestEntry] = []
        fatal: DeliveryError | None = None
        try:
            for email in emails:
                if fatal is not None or self._clock() > deadline:
                    entries.append(self._unsent(email, fatal))
                    continue
                entry, fatal = self._deliver_one(delivery, email, marker)
                entries.append(entry)
                if plan.jitter_ms:
                    self._sleeper(rng.uniform(0, plan.jitter_ms) / 1000.0)
        finally:
            delivery.close()
        return entries

    def _deliver_one(
        self, delivery: Delivery, email: PlannedEmail, marker: Marker
    ) -> tuple[ManifestEntry, DeliveryError | None]:
        """Compose and deliver one email; return its entry and any fatal error that stops the lane."""
        started = self._clock()
        composed = compose(self._templates[email.kind], email.address, field=email.field, marker=marker,
                           sent_at=datetime.now(UTC), secret=self._secret)
        common = {"index": email.index, "address": composed.address, "field": str(email.field),
                  "kind": str(email.kind), "link": composed.link, "jti": composed.jti,
                  "message_id": composed.message_id}
        try:
            attempts = deliver_with_retry(delivery, composed, budget_sec=SEND_BUDGET_SEC,
                                          clock=self._clock, sleeper=self._sleeper)
        except DeliveryError as error:
            _LOGGER.error("%s: email %d to %s: %s", error.primary, email.index, composed.address, error)
            entry = ManifestEntry(**common, status="failed", keyword=error.primary, error=str(error),
                                  duration_ms=int((self._clock() - started) * 1000))
            return entry, error if error.primary in UNREACHABLE_KEYWORDS else None
        entry = ManifestEntry(**common, status="sent", attempts=attempts,
                              duration_ms=int((self._clock() - started) * 1000))
        return entry, None

    @staticmethod
    def _unsent(email: PlannedEmail, fatal: DeliveryError | None) -> ManifestEntry:
        """Entry for an email not attempted: the server is unreachable, or the batch budget ran out."""
        keyword = fatal.primary if fatal is not None else SenderKeyword.BATCH_TIMEOUT
        reason = "not attempted: server unreachable" if fatal is not None else "batch time budget exceeded"
        return ManifestEntry(index=email.index, address=email.address, field=str(email.field),
                             kind=str(email.kind), status="failed", keyword=keyword, error=reason)

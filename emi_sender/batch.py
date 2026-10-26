# SPDX-FileCopyrightText: 2026 Aleksandr Polskiy
# SPDX-License-Identifier: Apache-2.0
"""Turn a batch request into a concrete per-email plan (design §6.3, §6.5)."""

import logging
import random
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum

from emi_sender.compose import Field, Kind, validate_address
from emi_sender.errors import InvalidInputError
from emi_sender.keywords import SenderCaseKeyword, SenderKeyword

_LOGGER = logging.getLogger(__name__)
MAX_PER_KIND = 100
MAX_TOTAL = 1000
MAX_ADDRESSES = 100
MAX_CONCURRENCY = 32
_UNEVEN_WEIGHTS = (4, 3, 2, 1, 0)


class Order(StrEnum):
    """Send order."""

    FIXED = "fixed"
    RANDOM = "random"


class Preset(StrEnum):
    """Named distributions of emails over addresses (AMB-36, AMB-45)."""

    ALL_TO_ONE = "all_to_one"
    EVEN_5 = "even_5"
    UNEVEN_5 = "uneven_5"
    ONE_EACH_10 = "one_each_10"


_PRESET_ADDRESSES = {Preset.ALL_TO_ONE: 1, Preset.EVEN_5: 5, Preset.UNEVEN_5: 5, Preset.ONE_EACH_10: 10}


@dataclass(frozen=True)
class PlannedEmail:
    """One email of a batch.

    Attributes:
        index: Position in the send order.
        address: Recipient.
        kind: Email kind.
        field: Recipient field carrying the address.
    """

    index: int
    address: str
    kind: Kind
    field: Field


@dataclass(frozen=True, kw_only=True)
class BatchRequest:  # pylint: disable=too-many-instance-attributes
    """What to send, before validation and planning.

    Attributes:
        addresses: Candidate recipient addresses.
        counts: Emails per kind; negative values are clamped to 0 (and logged).
        preset: How emails are spread over addresses; ignored when ``plan`` is given.
        explicit: Per-address counts per kind, overriding the preset.
        order: ``fixed`` or ``random``.
        seed: Seed for every random choice; generated and logged when ``None``.
        recipient_field: Recipient field for every email.
        concurrency: Parallel SMTP connections (1-32).
        jitter_ms: Random delay between sends, 0..jitter_ms.
    """

    addresses: tuple[str, ...]
    counts: Mapping[str, object] = field(default_factory=dict)
    preset: Preset = Preset.ALL_TO_ONE
    explicit: Mapping[str, Mapping[str, object]] | None = None
    order: Order = Order.FIXED
    seed: int | None = None
    recipient_field: Field = Field.TO
    concurrency: int = 1
    jitter_ms: int = 0


@dataclass(frozen=True)
class BatchPlan:
    """The resolved plan; logged and written to the manifest so any mix can be replayed.

    Attributes:
        emails: Emails in send order.
        seed: The seed actually used.
        concurrency: Parallel connections.
        jitter_ms: Random delay between sends.
        clamped: Fields whose negative count was clamped to 0.
    """

    emails: tuple[PlannedEmail, ...]
    seed: int
    concurrency: int
    jitter_ms: int
    clamped: tuple[str, ...] = ()


def _invalid(message: str, **details: object) -> InvalidInputError:
    """Build an invalid-input error (CLI exit code 2)."""
    return InvalidInputError(message, SenderCaseKeyword.CONDITION_INVALID_INPUT, details=details)


def validate_count(name: str, value: object, clamped: list[str]) -> int:
    """Validate one count: integers only; negative → 0 with the exception logged; above range → error.

    Args:
        name: Field name, for messages.
        value: The given value.
        clamped: Collects names of clamped fields.

    Returns:
        The usable count.

    Raises:
        InvalidInputError: Non-integer or above ``MAX_PER_KIND``.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise _invalid(f"{name} must be an integer, got {value!r}", field=name, value=repr(value))
    if value < 0:
        try:
            raise ValueError(f"{name} is negative ({value}); no emails are sent for it")
        except ValueError:
            _LOGGER.exception("%s: %s=%d clamped to 0", SenderKeyword.NEGATIVE_COUNT, name, value)
        clamped.append(name)
        return 0
    if value > MAX_PER_KIND:
        raise _invalid(f"{name} must be 0-{MAX_PER_KIND}, got {value}", field=name, value=value)
    return value


def _check_range(name: str, value: int, low: int, high: int) -> None:
    """Reject an integer setting outside ``[low, high]``."""
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise _invalid(f"{name} must be {low}-{high}, got {value!r}", field=name, value=repr(value))


def _kinds(counts: Mapping[str, object], prefix: str, clamped: list[str]) -> list[Kind]:
    """Expand validated counts into a list of kinds."""
    kinds: list[Kind] = []
    for key, value in counts.items():
        try:
            kind = Kind(key)
        except ValueError as error:
            raise _invalid(f"unknown kind {key!r}", field=f"{prefix}{key}") from error
        kinds.extend([kind] * validate_count(f"{prefix}{key}", value, clamped))
    return kinds


def _shares(total: int, weights: list[int]) -> list[int]:
    """Split ``total`` proportionally to ``weights`` (largest remainder)."""
    weight_sum = sum(weights) or 1
    exact = [total * weight / weight_sum for weight in weights]
    shares = [int(value) for value in exact]
    by_remainder = sorted(range(len(weights)), key=lambda position: exact[position] - shares[position],
                          reverse=True)
    for position in by_remainder[: total - sum(shares)]:
        shares[position] += 1
    return shares


def _assign(request: BatchRequest, addresses: list[str], kinds: list[Kind], rng: random.Random) -> list[str]:
    """Pick the address of each email according to the preset, in seeded random order."""
    needed = _PRESET_ADDRESSES[request.preset]
    if len(addresses) < needed:
        raise _invalid(f"preset {request.preset} needs {needed} addresses, got {len(addresses)}",
                       field="preset")
    chosen = addresses[:needed]
    if request.preset is Preset.ONE_EACH_10 and len(kinds) != needed:
        raise _invalid(f"preset {request.preset} needs exactly {needed} emails, got {len(kinds)}",
                       field="counts")
    weights = list(_UNEVEN_WEIGHTS) if request.preset is Preset.UNEVEN_5 else [1] * needed
    rng.shuffle(weights)  # so the first address does not always get the most
    targets: list[str] = []
    for address, share in zip(chosen, _shares(len(kinds), weights), strict=True):
        targets.extend([address] * share)
    rng.shuffle(targets)
    return targets


def plan_batch(request: BatchRequest) -> BatchPlan:
    """Validate a request and resolve it into a per-email plan.

    Args:
        request: The batch request.

    Returns:
        The plan, with the seed that reproduces it.

    Raises:
        InvalidInputError: On any invalid value (CLI exit code 2).
    """
    _check_range("concurrency", request.concurrency, 1, MAX_CONCURRENCY)
    _check_range("jitter_ms", request.jitter_ms, 0, 60_000)
    addresses = [validate_address(address) for address in request.addresses]
    if not 1 <= len(addresses) <= MAX_ADDRESSES:
        raise _invalid(f"1-{MAX_ADDRESSES} addresses required, got {len(addresses)}", field="addresses")
    seed = request.seed if request.seed is not None else random.SystemRandom().randrange(2**31)
    rng = random.Random(seed)
    clamped: list[str] = []
    pairs: list[tuple[str, Kind]] = []
    if request.explicit is not None:
        for address, counts in request.explicit.items():
            target = validate_address(address)
            pairs.extend((target, kind) for kind in _kinds(counts, f"{target}.", clamped))
    else:
        kinds = _kinds(request.counts, "", clamped)
        rng.shuffle(kinds)
        pairs = list(zip(_assign(request, addresses, kinds, rng), kinds, strict=True))
    if len(pairs) > MAX_TOTAL:
        raise _invalid(f"at most {MAX_TOTAL} emails per batch, got {len(pairs)}", field="counts")
    if request.order is Order.RANDOM:
        rng.shuffle(pairs)
    emails = tuple(PlannedEmail(index, address, kind, request.recipient_field)
                   for index, (address, kind) in enumerate(pairs))
    _LOGGER.info("batch plan: %d emails, seed=%d, order=%s, preset=%s", len(emails), seed,
                 request.order, request.preset)
    return BatchPlan(emails=emails, seed=seed, concurrency=request.concurrency,
                     jitter_ms=request.jitter_ms, clamped=tuple(clamped))

# SPDX-FileCopyrightText: 2026 Aleksandr Polskiy
# SPDX-License-Identifier: Apache-2.0
"""Unit tests: batch planning, distributions and input validation (design §6.3, §6.5)."""

import logging
from collections import Counter

import pytest

from emi_sender import BatchRequest, InvalidInputError, Kind, Order, Preset, SenderKeyword, plan_batch

_ADDRESSES = tuple(f"qa+400100_run-{index:02d}@example.com" for index in range(10))
_MIX = {"activation": 4, "invitation": 3, "generic": 3}


def _per_address(request: BatchRequest) -> Counter[str]:
    """How many emails each address receives."""
    return Counter(email.address for email in plan_batch(request).emails)


class TestEMISenderDistributions:
    """The four required distributions, in seeded random order."""

    def EMI_SENDER_UNIT_110020_all_to_one(self) -> None:
        """Every email goes to the first address."""
        assert _per_address(BatchRequest(addresses=_ADDRESSES, counts=_MIX, seed=1)) == {_ADDRESSES[0]: 10}

    def EMI_SENDER_UNIT_110021_even_5(self) -> None:
        """Five addresses share ten emails evenly; the other five get none."""
        shares = _per_address(BatchRequest(addresses=_ADDRESSES, counts=_MIX, preset=Preset.EVEN_5, seed=1))
        assert sorted(shares.values()) == [2, 2, 2, 2, 2]
        assert set(shares) <= set(_ADDRESSES[:5])

    def EMI_SENDER_UNIT_110022_uneven_5_random_weights(self) -> None:
        """Weights 4, 3, 2, 1, 0 are assigned to the five addresses in seeded random order."""
        shares = [_per_address(BatchRequest(addresses=_ADDRESSES, counts=_MIX, preset=Preset.UNEVEN_5, seed=seed))
                  for seed in range(6)]
        assert all(sorted(share.values()) == [1, 2, 3, 4] for share in shares)
        assert len({share.most_common(1)[0][0] for share in shares}) > 1

    def EMI_SENDER_UNIT_110023_one_each_10(self) -> None:
        """Ten addresses, one email each; the kinds are mixed."""
        plan = plan_batch(BatchRequest(addresses=_ADDRESSES, counts=_MIX, preset=Preset.ONE_EACH_10, seed=3))
        assert sorted(Counter(email.address for email in plan.emails).values()) == [1] * 10
        assert Counter(email.kind for email in plan.emails) == {Kind.ACTIVATION: 4, Kind.INVITATION: 3,
                                                               Kind.GENERIC: 3}

    def EMI_SENDER_UNIT_110024_seed_reproduces_plan(self) -> None:
        """Same seed, same plan; the seed is reported when not given."""
        request = BatchRequest(addresses=_ADDRESSES, counts=_MIX, preset=Preset.UNEVEN_5, order=Order.RANDOM,
                               seed=42)
        assert plan_batch(request).emails == plan_batch(request).emails
        assert isinstance(plan_batch(BatchRequest(addresses=_ADDRESSES, counts=_MIX)).seed, int)

    def EMI_SENDER_UNIT_110025_explicit_plan_overrides_preset(self) -> None:
        """Per-address counts are used exactly."""
        explicit = {_ADDRESSES[0]: {"activation": 2}, _ADDRESSES[1]: {"invitation": 1, "generic": 1}}
        shares = _per_address(BatchRequest(addresses=_ADDRESSES, explicit=explicit, preset=Preset.EVEN_5))
        assert shares == {_ADDRESSES[0]: 2, _ADDRESSES[1]: 2}


class TestEMISenderValidation:
    """Negative counts send nothing; anything else invalid is rejected (exit code 2)."""

    def EMI_SENDER_UNIT_110030_negative_count_sends_nothing_and_logs(self, caplog: pytest.LogCaptureFixture) -> None:
        """A negative count becomes 0, the exception is logged with its keyword, the rest is sent."""
        with caplog.at_level(logging.ERROR):
            plan = plan_batch(BatchRequest(addresses=_ADDRESSES[:1], counts={"activation": -3, "generic": 2}))
        assert Counter(email.kind for email in plan.emails) == {Kind.GENERIC: 2}
        assert plan.clamped == ("activation",)
        assert SenderKeyword.NEGATIVE_COUNT in caplog.text and "Traceback" in caplog.text

    def EMI_SENDER_UNIT_110031_negative_never_reaches_a_send(self) -> None:
        """Only negative counts: the plan is empty."""
        assert not plan_batch(BatchRequest(addresses=_ADDRESSES[:1], counts={"activation": -1})).emails

    @pytest.mark.parametrize("bad_count", ["abc", 2.5, True, None, 101])
    def EMI_SENDER_UNIT_110032_non_integer_or_out_of_range_count_rejected(self, bad_count: object) -> None:
        """Non-integers (including booleans) and counts above 100 are invalid input."""
        with pytest.raises(InvalidInputError) as caught:
            plan_batch(BatchRequest(addresses=_ADDRESSES[:1], counts={"activation": bad_count}))
        assert caught.value.primary == "CASE_CONDITION_INVALID_INPUT"

    @pytest.mark.parametrize("bad_kwargs", [{"concurrency": 0}, {"concurrency": 33}, {"jitter_ms": -1},
                                            {"addresses": ()}, {"preset": Preset.EVEN_5},
                                            {"counts": {"reminder": 1}}])
    def EMI_SENDER_UNIT_110033_invalid_settings_rejected(self, bad_kwargs: dict[str, object]) -> None:
        """Out-of-range concurrency/jitter, no addresses, too few addresses, unknown kind."""
        values: dict[str, object] = {"addresses": _ADDRESSES[:1], "counts": {"activation": 1}}
        values.update(bad_kwargs)
        with pytest.raises(InvalidInputError):
            plan_batch(BatchRequest(**values))  # type: ignore[arg-type]

    def EMI_SENDER_UNIT_110034_total_above_1000_rejected(self) -> None:
        """At most 1,000 emails per batch."""
        explicit = {address: {"activation": 100, "invitation": 1} for address in _ADDRESSES}
        with pytest.raises(InvalidInputError):
            plan_batch(BatchRequest(addresses=_ADDRESSES, explicit=explicit))

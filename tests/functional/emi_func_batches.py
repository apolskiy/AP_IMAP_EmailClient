# SPDX-FileCopyrightText: 2026 Aleksandr Polskiy
# SPDX-License-Identifier: Apache-2.0
"""Functional tests: batches of 10 over 1, 5 or 10 addresses (design §6.3, §13.3).

Every distribution preset x send order x recipient field x kind mix x concurrency x seed. Each case sends
one seeded batch to its own addresses (slots of its own case ID) and checks that every address received
exactly the emails planned for it, and that the preset's shape holds.
"""

import itertools
from collections import Counter
from dataclasses import dataclass

import pytest

from emi_api import EmiApi, EmiRun
from emi_harness import Steps
from emi_sender import BatchRequest, Field, Order, Preset
from tests.functional.catalog import FIELDS, MATCH_FIELDS, cases

_ADDRESSES = {Preset.ALL_TO_ONE: 1, Preset.EVEN_5: 5, Preset.UNEVEN_5: 5, Preset.ONE_EACH_10: 10}
_SHAPE = {Preset.ALL_TO_ONE: [10], Preset.EVEN_5: [2, 2, 2, 2, 2], Preset.UNEVEN_5: [0, 1, 2, 3, 4],
          Preset.ONE_EACH_10: [1] * 10}
_MIXES = {"activation-10": {"activation": 10},
          "mixed-4-3-3": {"activation": 4, "invitation": 3, "generic": 3},
          "invitation-generic-5-5": {"invitation": 5, "generic": 5}}
_PRIORITY = {Preset.ALL_TO_ONE: "P2", Preset.EVEN_5: "P2", Preset.UNEVEN_5: "P3", Preset.ONE_EACH_10: "P4"}


@dataclass(frozen=True)
class Batch:
    """One batch case."""

    preset: Preset
    order: Order
    field: str
    mix: str
    concurrency: int
    seed: int

    @property
    def name(self) -> str:
        """Readable part of the pytest ID."""
        return f"{self.preset}-{self.order}-{self.field}-{self.mix}-c{self.concurrency}-s{self.seed}"


_BATCHES = [(Batch(*value),) for value in itertools.product(Preset, Order, FIELDS, _MIXES, (1, 4), range(1, 7))]


class TestEMIBatches:
    """A batch arrives complete, each email at its planned address."""

    @pytest.mark.parametrize("batch", cases(410000, _BATCHES, name=lambda value: value[0].name,
                                            priority=lambda value: _PRIORITY[value[0].preset]))
    def EMI_TOOL_FUNC_410000_batch_arrives_as_planned(self, api: EmiApi, steps: Steps, emi_run: EmiRun,
                                                      batch: Batch) -> None:
        """Every address gets exactly its planned emails; the kinds and the preset's shape are as asked."""
        addresses = emi_run.addresses(_ADDRESSES[batch.preset])
        with steps.step(f"Send 10 emails: {batch.name}", timeout=30) as step:
            manifest = api.sender.send_batch(BatchRequest(
                addresses=addresses, counts=_MIXES[batch.mix], preset=batch.preset, order=batch.order,
                seed=batch.seed, recipient_field=Field(batch.field), concurrency=batch.concurrency),
                marker=emi_run.marker)
            step.verify("10 sent", len(manifest.sent) == 10)
            step.verify("the seed is recorded", manifest.seed == batch.seed)
            planned = Counter(entry.address for entry in manifest.sent)
            step.verify(f"{batch.preset} shape",
                        sorted(planned.get(address, 0) for address in addresses) == _SHAPE[batch.preset])
            step.verify("kinds as asked", Counter(entry.kind for entry in manifest.sent) == Counter(_MIXES[batch.mix]))
        with steps.step("Each address receives exactly its planned emails",
                        timeout=api.settings.wait_timeout + 30) as step:
            for address, count in sorted(planned.items()):
                emails = api.reader.wait_for_emails(address, count=count, timeout=step.remaining,
                                                    match_fields=MATCH_FIELDS[batch.field], settle_window=0.0)
                expected = {entry.message_id for entry in manifest.sent if entry.address == address}
                step.verify(f"{address}: the planned {count}", {item.message_id for item in emails} == expected)

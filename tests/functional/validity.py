# SPDX-FileCopyrightText: 2026 Aleksandr Polskiy
# SPDX-License-Identifier: Apache-2.0
"""Which functional tests must catch each named fault, and how (design §13.4, AMB-64).

``python -m emi_harness.validity faults`` runs each fault's tests under that fault: every one must fail
with one of the listed keywords. Case IDs are looked up in the families' own parameter lists, so reordering
or extending a family cannot silently point a catch at another test. A new family adds the faults it catches.
"""

from collections.abc import Sequence
from typing import Any

from emi_harness.validity import Catch
from emi_reader import ReaderCaseKeyword
from emi_sender import Order, Preset
from tests.functional import emi_func_batches, emi_func_counts, emi_func_fields, emi_func_isolation
from tests.functional import emi_func_markers, emi_func_timing
from tests.functional.emi_func_batches import Batch

_NOT_RECEIVED = ReaderCaseKeyword.EMAIL_NOT_RECEIVED
_EXCEEDED = ReaderCaseKeyword.EMAIL_COUNT_EXCEEDED
_VERIFICATION = "CASE_VERIFICATION_FAILED"


def _case(first: int, values: Sequence[Sequence[Any]], *wanted: Any) -> str:
    """The case ID of the parameter set ``wanted`` in a family that starts at ``first``."""
    return f"C{first + [tuple(value) for value in values].index(tuple(wanted))}"


def _counts(kind: str, field: str, count: int) -> str:
    """A counts case (401000 block)."""
    return _case(401000, emi_func_counts.COUNTS, kind, field, count)


def _batch(preset: Preset, field: str, mix: str, *, concurrency: int = 1, seed: int = 1) -> str:
    """A batch case with fixed order (410000 block)."""
    return _case(410000, emi_func_batches.BATCHES, Batch(preset, Order.FIXED, field, mix, concurrency, seed))


CATCHES: dict[str, tuple[Catch, ...]] = {
    "sender-drops-email": (
        # send and send_batch both drop one email (the counts family sends batches).
        Catch(("C400001", "C400002", "C400006", "C400007", "C400008",
               _counts("activation", "to", 3), _counts("invitation", "cc", 2),
               _counts("generic", "bcc", 1), _case(403000, emi_func_markers.FORM_CASES, "activation", "case-id"),
               _case(404000, emi_func_timing.CASES, "invitation", 0.5)), (_NOT_RECEIVED,)),
        # One email fewer arrives, so "one more than expected" is never reached: the expected error is missing.
        Catch((_case(401500, emi_func_counts.EXCEEDED, "activation", 1),
               _case(401500, emi_func_counts.EXCEEDED, "generic", 2)), (_VERIFICATION,)),
    ),
    "sender-ignores-field": (
        Catch((_counts("activation", "cc", 1), _counts("invitation", "bcc", 2),
               _batch(Preset.ALL_TO_ONE, "cc", "activation-10"), _batch(Preset.EVEN_5, "bcc", "mixed-4-3-3")),
              (_NOT_RECEIVED,)),
        # Sent in To instead of Cc/Bcc, so the To-only wait that must find nothing finds it.
        Catch((_case(402000, emi_func_fields.CASES, "activation", "cc", "to"),
               _case(402000, emi_func_fields.CASES, "invitation", "bcc", "to"),
               _case(402100, emi_func_fields.COMBINATION_CASES, "generic", "cc+bcc"), "C402200"),
              (_VERIFICATION,)),
    ),
    "reader-any-field": (
        Catch((*(_case(402000, emi_func_fields.CASES, *value) for value in emi_func_fields.CASES),
               *(_case(402100, emi_func_fields.COMBINATION_CASES, *value)
                 for value in emi_func_fields.COMBINATION_CASES), "C402201"),
              (_VERIFICATION,)),
    ),
    "reader-ignores-markers": (
        Catch(tuple(_case(403012, emi_func_markers.OTHER_RUN_CASES, *value)
                    for value in emi_func_markers.OTHER_RUN_CASES), (_VERIFICATION,)),
    ),
    "reader-slot-collapse": (
        Catch((_case(406000, emi_func_isolation.CASES, "activation", "own-extra-slot"),
               _case(406000, emi_func_isolation.CASES, "generic", "own-extra-slot"),
               _batch(Preset.ALL_TO_ONE, "to", "activation-10"), _batch(Preset.EVEN_5, "to", "activation-10"),
               _batch(Preset.UNEVEN_5, "cc", "mixed-4-3-3"), _batch(Preset.ONE_EACH_10, "bcc", "activation-10")),
              (_NOT_RECEIVED, _EXCEEDED, _VERIFICATION)),
    ),
    "reader-duplicates-email": (
        Catch((_counts("activation", "to", 2), _counts("generic", "cc", 5),
               _batch(Preset.ALL_TO_ONE, "to", "activation-10"), _batch(Preset.EVEN_5, "to", "mixed-4-3-3")),
              (_VERIFICATION,)),
    ),
    "reader-returns-extra": (
        Catch((_counts("activation", "to", 1), _counts("invitation", "to", 3),
               _batch(Preset.ALL_TO_ONE, "to", "activation-10"), _batch(Preset.ONE_EACH_10, "to", "activation-10")),
              (_VERIFICATION,)),
    ),
    "reader-wrong-link": (
        Catch(("C400001", "C400002", "C400007", "C400008", _counts("activation", "to", 1),
               _counts("invitation", "cc", 2)), (_VERIFICATION,)),
    ),
    "reader-gives-up-early": (
        Catch(tuple(_case(404000, emi_func_timing.CASES, kind, delay)
                    for kind in ("activation", "invitation") for delay in (2.0, 5.0)), (_NOT_RECEIVED,)),
    ),
}

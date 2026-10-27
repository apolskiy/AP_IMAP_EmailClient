# SPDX-FileCopyrightText: 2026 Aleksandr Polskiy
# SPDX-License-Identifier: Apache-2.0
"""TestRail case IDs for parametrized functional families, and shared lookups (design §13.3).

Every parametrized case is its own TestRail case: :func:`cases` gives the n-th case ``C<first + n>``
through its own ``emi`` marker, so the case can be run, reported and searched on its own
(``--test C410123``). The harness refuses two cases with one ID (``HARNESS_DUPLICATE_TEST_ID``), which also
keeps the ID blocks below from overlapping.

ID blocks (functional range 400000-599999):

=================  ===========================================  ==========
Block              Family                                       Cases
=================  ===========================================  ==========
400001-400005      delivery basics (``emi_func_delivery``)       5
401000-401089      counts per kind and field (``emi_func_counts``)  90
401500-401508      count exceeded (``emi_func_counts``)           9
402000-402008      recipient fields not matched (``emi_func_fields``)  9
403000-403014      markers (``emi_func_markers``)                 15
404000-404014      arrival during the wait (``emi_func_timing``)  15
406000-406008      isolation (``emi_func_isolation``)             9
410000-410863      batches (``emi_func_batches``)                 864
=================  ===========================================  ==========
"""

from collections.abc import Callable, Iterable, Sequence
from typing import Any

import pytest

from emi_reader import Cta

KINDS = ("activation", "invitation", "generic")
FIELDS = ("to", "cc", "bcc")
CTA_OF: dict[str, Cta | None] = {"activation": Cta.ACTIVATE_ACCOUNT, "invitation": Cta.ACCEPT_INVITATION,
                                 "generic": None}
# A Bcc recipient is invisible in received headers; the sender stamps Delivered-To, as Gmail does.
MATCH_FIELDS: dict[str, tuple[str, ...]] = {"to": ("to",), "cc": ("cc",), "bcc": ("delivered-to",)}
NEGATIVE_WAIT_SEC = 3.0


def cases(
    first: int,
    values: Iterable[Sequence[Any]],
    *,
    name: Callable[[Sequence[Any]], str],
    priority: Callable[[Sequence[Any]], str | None] = lambda _value: None,
) -> list[Any]:
    """One ``pytest.param`` per value, each with its own TestRail case ID.

    Args:
        first: The first case number; the n-th value gets ``C<first + n>``.
        values: Argument tuples, one per case.
        name: The readable part of each case's pytest ID.
        priority: The case's priority, or None to keep the function's.

    Returns:
        The parameters for ``pytest.mark.parametrize``.
    """
    params: list[Any] = []
    for index, value in enumerate(values):
        case = f"C{first + index}"
        extra = {"priority": level} if (level := priority(value)) else {}
        params.append(pytest.param(*value, id=f"{case}-{name(value)}",
                                   marks=pytest.mark.emi(case=case, **extra)))
    return params

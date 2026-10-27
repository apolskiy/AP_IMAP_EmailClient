# SPDX-FileCopyrightText: 2026 Aleksandr Polskiy
# SPDX-License-Identifier: Apache-2.0
"""Mailbox inventory: folder totals and per-category counts at a test's address (design §5.8, AMB-76).

Taken from each wait at no extra IMAP cost: the wait's own ``EXAMINE`` gives the folder totals, and every
email it fetches at the address is classified. Never used for pass/fail; written for analysis across runs.
"""

import re
from dataclasses import dataclass, field
from typing import Any

from emi_reader.links import Cta, html_anchor_links, text_links
from emi_reader.mime import ParsedEmail

ACTIVATION = "activation"
INVITATION = "invitation"
NO_LINKS = "no_links"
UNCLASSIFIED = "unclassified"
CATEGORIES = (ACTIVATION, INVITATION, NO_LINKS, UNCLASSIFIED)
_KINDS = ((Cta.ACTIVATE_ACCOUNT, ACTIVATION), (Cta.ACCEPT_INVITATION, INVITATION))
_ANY_LINK = re.compile(r"https?://", re.IGNORECASE)


def classify(email: ParsedEmail) -> str:
    """The email's category: its first known CTA, ``no_links`` without any link, else ``unclassified``.

    Args:
        email: The parsed email.

    Returns:
        One of ``CATEGORIES``.
    """
    for cta, name in _KINDS:
        if (email.html and html_anchor_links(email.html, cta)) or (email.text and text_links(email.text, cta)):
            return name
    bodies = (email.html or "") + (email.text or "")
    return UNCLASSIFIED if _ANY_LINK.search(bodies) else NO_LINKS


@dataclass
class Snapshot:
    """Folder totals and the categories of the emails at the address at one moment.

    Attributes:
        folders: Folder name -> ``{"messages": n, "uidnext": n}``.
        categories: Category -> count of emails at the address.
    """

    folders: dict[str, dict[str, int]] = field(default_factory=dict)
    categories: dict[str, int] = field(default_factory=lambda: dict.fromkeys(CATEGORIES, 0))

    def to_json(self) -> dict[str, Any]:
        """As plain JSON.

        Returns:
            Folders with their totals, summed totals and the category counts.
        """
        return {"folders": {name: dict(values) for name, values in self.folders.items()},
                "total_messages": sum(values["messages"] for values in self.folders.values()),
                "categories": dict(self.categories)}


@dataclass
class Inventory:
    """One wait's inventory, before and after.

    Attributes:
        address: The address the wait was for.
        match_fields: The recipient fields the wait searched (the categories cover only those).
        before: When the wait started: totals and the emails already at the address.
        after: When it ended: totals and every email it saw at the address.
    """

    address: str
    match_fields: tuple[str, ...]
    before: Snapshot = field(default_factory=Snapshot)
    after: Snapshot = field(default_factory=Snapshot)

    def to_json(self) -> dict[str, Any]:
        """As plain JSON.

        Returns:
            Address, fields, before and after.
        """
        return {"address": self.address, "match_fields": list(self.match_fields),
                "before": self.before.to_json(), "after": self.after.to_json()}

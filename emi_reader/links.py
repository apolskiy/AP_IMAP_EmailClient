# SPDX-FileCopyrightText: 2026 Aleksandr Polskiy
# SPDX-License-Identifier: Apache-2.0
"""Find the link behind an email's call-to-action button (design §5.6)."""

import html as html_lib
import logging
import re
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from typing import ClassVar
from urllib.parse import urlsplit

from bs4 import BeautifulSoup
from bs4.element import Tag

from emi_reader.errors import LinkExtractionError
from emi_reader.keywords import ReaderCaseKeyword, ReaderKeyword
from emi_reader.mime import ParsedEmail

_LOGGER = logging.getLogger(__name__)
_URL_PATTERN = re.compile(r"https?://[^\s<>\"']+")


@dataclass(frozen=True)
class Cta:
    """A call-to-action button: its label in HTML and the path its link must have.

    Attributes:
        label: Visible button text, matched exactly after normalization.
        url_pattern: Regular expression searched in the link's path; used for the plain-text
            fallback and to reject footer links.
    """

    label: str
    url_pattern: str

    ACTIVATE_ACCOUNT: ClassVar["Cta"]
    ACCEPT_INVITATION: ClassVar["Cta"]

    def path_matches(self, url: str) -> bool:
        """Check whether a URL's path matches this CTA.

        Args:
            url: Absolute URL.

        Returns:
            True when ``url_pattern`` is found in the URL's path.
        """
        return re.search(self.url_pattern, urlsplit(url).path) is not None


Cta.ACTIVATE_ACCOUNT = Cta(label="Activate account", url_pattern=r"^/activate/?$")
Cta.ACCEPT_INVITATION = Cta(label="Accept invitation", url_pattern=r"^/invite/accept/?$")


def normalize_label(value: str) -> str:
    """Normalize visible text for exact comparison: NFKC, case-fold, collapse whitespace.

    Args:
        value: Raw text.

    Returns:
        The normalized text.
    """
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def _anchor_text(anchor: Tag) -> str:
    """Visible text of an anchor, including ``alt`` text of images inside it.

    Args:
        anchor: An ``<a>`` element.

    Returns:
        The combined text.
    """
    pieces = [anchor.get_text(" ")]
    pieces.extend(str(image.get("alt", "")) for image in anchor.find_all("img"))
    return " ".join(pieces)


def html_anchor_links(html_body: str, cta: Cta) -> list[str]:
    """Strategy 1: ``<a href>`` elements whose normalized text equals the CTA label.

    Args:
        html_body: Decoded HTML.
        cta: The button to find.

    Returns:
        Distinct matching URLs in document order; buttons with ``class="btn"`` first.
    """
    soup = BeautifulSoup(html_body, "html.parser")
    wanted = normalize_label(cta.label)
    preferred: list[str] = []
    others: list[str] = []
    for anchor in soup.find_all("a", href=True):
        if normalize_label(_anchor_text(anchor)) != wanted:
            continue
        url = html_lib.unescape(str(anchor["href"]).strip())
        target = preferred if "btn" in (anchor.get("class") or []) else others
        if url not in preferred and url not in others:
            target.append(url)
    return preferred + others


def text_links(text_body: str, cta: Cta) -> list[str]:
    """Plain-text fallback: URLs whose path matches the CTA's ``url_pattern``.

    Args:
        text_body: Decoded plain text.
        cta: The button to find.

    Returns:
        Distinct matching URLs in order of appearance.
    """
    found: list[str] = []
    for url in _URL_PATTERN.findall(text_body):
        if cta.path_matches(url) and url not in found:
            found.append(url)
    return found


@dataclass(frozen=True)
class LinkResult:
    """Outcome of an extraction.

    Attributes:
        url: The CTA link.
        source: ``"html"`` or ``"text"``.
        secondary: Secondary keywords recorded on the way (e.g. no HTML part).
        warnings: Human-readable warnings (e.g. a mismatch in warn mode).
    """

    url: str
    source: str
    secondary: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()


# A strategy takes the email and the CTA and returns candidate URLs (empty when it does not apply).
Strategy = Callable[[ParsedEmail, Cta], list[str]]


def _html_strategy(email: ParsedEmail, cta: Cta) -> list[str]:
    """Run the HTML anchor strategy when the email has an HTML part."""
    return html_anchor_links(email.html, cta) if email.html else []


class LinkExtractor:
    """Pluggable chain of strategies; the first one that finds links wins.

    New strategies (form buttons, Outlook VML, …) are added with :meth:`register` without changing
    callers. The plain-text part is always used as the fallback and as the cross-check.
    """

    def __init__(self, *, mismatch_is_error: bool = True) -> None:
        """Create an extractor with the built-in HTML strategy.

        Args:
            mismatch_is_error: When True, differing HTML and text links fail the extraction
                (possible phishing); when False they only produce a warning.
        """
        self._strategies: list[tuple[str, Strategy]] = [("html", _html_strategy)]
        self._mismatch_is_error = mismatch_is_error

    def register(self, name: str, strategy: Strategy) -> None:
        """Append a strategy to the chain.

        Args:
            name: Name reported as the link's source.
            strategy: Callable returning candidate URLs.
        """
        self._strategies.append((name, strategy))

    def extract(self, email: ParsedEmail, cta: Cta) -> LinkResult:
        """Find the CTA link in an email.

        Args:
            email: The parsed email.
            cta: The button to find.

        Returns:
            The link and how it was found.

        Raises:
            LinkExtractionError: Not found, ambiguous, or HTML and text disagree.
        """
        secondary: list[str] = []
        if email.html is None:
            secondary.append(ReaderKeyword.PARSE_NO_HTML_PART)
        text_candidates = text_links(email.text, cta) if email.text else []
        for name, strategy in self._strategies:
            candidates = strategy(email, cta)
            if candidates:
                url = self._single(candidates, cta, name)
                return self._cross_check(url, name, text_candidates, tuple(secondary))
        if text_candidates:
            url = self._single(text_candidates, cta, "text")
            return LinkResult(url=url, source="text", secondary=tuple(secondary))
        raise LinkExtractionError(
            f"no '{cta.label}' link found",
            ReaderCaseKeyword.LINK_NOT_FOUND,
            secondary=tuple(secondary),
            details={"subject": email.subject, "message_id": email.message_id},
        )

    @staticmethod
    def _single(candidates: list[str], cta: Cta, source: str) -> str:
        """Return the only candidate, or fail as ambiguous."""
        if len(candidates) > 1:
            raise LinkExtractionError(
                f"{len(candidates)} different '{cta.label}' links in the {source} part",
                ReaderCaseKeyword.LINK_AMBIGUOUS,
                details={"candidates": candidates},
            )
        return candidates[0]

    def _cross_check(
        self, url: str, source: str, text_candidates: list[str], secondary: tuple[str, ...]
    ) -> LinkResult:
        """Compare the chosen link with the plain-text link, if the text part has one."""
        if not text_candidates or url in text_candidates:
            return LinkResult(url=url, source=source, secondary=secondary)
        message = f"{source} link {url} differs from text link {text_candidates[0]}"
        if self._mismatch_is_error:
            raise LinkExtractionError(
                message,
                ReaderCaseKeyword.LINK_HTML_TEXT_MISMATCH,
                secondary=secondary,
                details={"html_link": url, "text_links": text_candidates},
            )
        _LOGGER.warning("%s: %s", ReaderCaseKeyword.LINK_HTML_TEXT_MISMATCH, message)
        return LinkResult(
            url=url,
            source=source,
            secondary=secondary + (ReaderCaseKeyword.LINK_HTML_TEXT_MISMATCH,),
            warnings=(message,),
        )


def extract_link(email: ParsedEmail, cta: Cta, *, mismatch_is_error: bool = True) -> LinkResult:
    """Extract a CTA link with the default strategy chain.

    Args:
        email: The parsed email.
        cta: The button to find.
        mismatch_is_error: See :class:`LinkExtractor`.

    Returns:
        The link and how it was found.
    """
    return LinkExtractor(mismatch_is_error=mismatch_is_error).extract(email, cta)

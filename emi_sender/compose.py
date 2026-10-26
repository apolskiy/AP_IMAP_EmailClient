# SPDX-FileCopyrightText: 2026 Aleksandr Polskiy
# SPDX-License-Identifier: Apache-2.0
"""Build realistic emails from the JSON templates and the sample ``.eml`` files (design §6.1, §6.2)."""

import html as html_lib
import json
import re
from dataclasses import dataclass
from datetime import datetime
from email import policy
from email.headerregistry import Address
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import format_datetime, make_msgid
from enum import StrEnum
from pathlib import Path

from emi_sender.errors import InvalidInputError
from emi_sender.keywords import SenderCaseKeyword
from emi_sender.tokens import TEST_ONLY_SECRET, find_token, reissue, token_claims

REPO_ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"
_IDENTIFIER = re.compile(r"^[A-Za-z0-9_-]+$")
_CASE_ID = re.compile(r"^[Cc]?(?P<number>\d{6,})$")
_ADDRESS = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")
_URL = re.compile(r"https?://[^\s\"'<>]+")
_BODY_END = re.compile(r"</body\s*>", re.IGNORECASE)
_FROM = Address(display_name="Example Cloud", addr_spec="noreply@cloud.example.com")
_UNDISCLOSED = "undisclosed-recipients:;"


class Kind(StrEnum):
    """Email kinds the sender produces."""

    GENERIC = "generic"
    ACTIVATION = "activation"
    INVITATION = "invitation"


class Field(StrEnum):
    """Recipient field the test address is placed in."""

    TO = "to"
    CC = "cc"
    BCC = "bcc"


@dataclass(frozen=True)
class Template:
    """A JSON template.

    Attributes:
        name: Template name.
        kind: Email kind.
        source_eml: Sample email the kind is built on (``None`` for generic).
        subject: Subject of a generic email.
        text: Plain-text body of a generic email (must contain no links).
        cta_label: Button label, for documentation and manifests.
    """

    name: str
    kind: Kind
    source_eml: Path | None = None
    subject: str = ""
    text: str = ""
    cta_label: str = ""


@dataclass(frozen=True)
class ComposedEmail:
    """A ready-to-send email and what the manifest needs to know about it.

    Attributes:
        message: The email.
        kind: Its kind.
        address: The test address it carries.
        field: Which recipient field carries the address.
        link: The CTA link (``None`` for generic).
        jti: The token's ``jti`` (``None`` for generic).
        message_id: The ``Message-ID`` header.
    """

    message: EmailMessage
    kind: Kind
    address: str
    field: Field
    link: str | None
    jti: str | None
    message_id: str


def _invalid(message: str, **details: object) -> InvalidInputError:
    """Build an invalid-input error."""
    return InvalidInputError(message, SenderCaseKeyword.CONDITION_INVALID_INPUT, details=details)


def load_template(path: Path) -> Template:
    """Load and validate one JSON template.

    Args:
        path: Template file.

    Returns:
        The template.

    Raises:
        InvalidInputError: Malformed JSON, unknown kind, missing sample, or a generic body with links.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        kind = Kind(data["kind"])
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise _invalid(f"template {path.name} is invalid: {error}", template=str(path)) from error
    source = REPO_ROOT / data["source_eml"] if data.get("source_eml") else None
    template = Template(name=str(data.get("name", path.stem)), kind=kind, source_eml=source,
                        subject=str(data.get("subject", "")), text=str(data.get("text", "")),
                        cta_label=str(data.get("cta_label", "")))
    if kind is Kind.GENERIC and _URL.search(template.text):
        raise _invalid(f"generic template {path.name} must contain no links", template=str(path))
    if kind is not Kind.GENERIC and (source is None or not source.is_file()):
        raise _invalid(f"template {path.name} needs an existing source_eml", template=str(path))
    return template


def builtin_templates() -> dict[Kind, Template]:
    """Load the templates shipped in ``emi_sender/templates``.

    Returns:
        One template per kind.
    """
    templates = {template.kind: template for template in
                 (load_template(path) for path in sorted(TEMPLATE_DIR.glob("*.json")))}
    return templates


def testrail_case_id(value: str) -> str | None:
    """The TestRail case ID form ``C<6+ digits>`` of a test ID, so mail maps to its TestRail case.

    Args:
        value: ``400001``, ``C400001`` or ``c400001``.

    Returns:
        ``C400001``, or None when the value is not a TestRail case ID.
    """
    match = _CASE_ID.match(value.strip())
    return f"C{match['number']}" if match else None


def validate_identifier(name: str, value: str) -> str:
    """Accept only ASCII ``[A-Za-z0-9_-]`` identifiers (test IDs, run IDs).

    Args:
        name: Field name for the error message.
        value: The value.

    Returns:
        The value.

    Raises:
        InvalidInputError: On anything else (e.g. Cyrillic).
    """
    if not _IDENTIFIER.match(value):
        raise _invalid(f"{name} must match [A-Za-z0-9_-]+, got {value!r}", field=name, value=value)
    return value


def validate_address(value: str) -> str:
    """Accept only a plain ASCII email address with a local part of at most 64 characters.

    Args:
        value: The address.

    Returns:
        The address with a lower-case domain; the local part keeps its case so the TestRail ``C`` in
        ``qa+C400001_...`` reaches the email (mail servers and the reader match recipients case-insensitively).

    Raises:
        InvalidInputError: If the address is not ASCII, malformed or too long.
    """
    local, separator, domain = value.strip().rpartition("@")
    address = f"{local}{separator}{domain.lower()}"
    if not address.isascii() or not _ADDRESS.match(address) or len(address.split("@")[0]) > 64:
        raise _invalid(f"invalid recipient address {value!r}", field="address", value=value)
    return address


def _marker_line(test_id: str, run_id: str) -> str:
    """The searchable marker line placed at the bottom of each body."""
    return f"EMI-Test: {test_id} run={run_id}"


def _set_recipient(message: EmailMessage, address: str, field: Field, display_name: str) -> None:
    """Put the test address into the requested field; Bcc leaves only a Delivered-To trace."""
    for header in ("To", "Cc", "Bcc", "Delivered-To"):
        del message[header]
    if field is Field.TO:
        message["To"] = Address(display_name=display_name, addr_spec=address)
        return
    message["To"] = _UNDISCLOSED
    if field is Field.CC:
        message["Cc"] = address
    else:
        # Bcc is invisible in the received headers; Gmail stamps Delivered-To. Emulated (AMB-07).
        message["Delivered-To"] = address


def _rewrite_part(part: EmailMessage, old_token: str, new_token: str, marker: str | None) -> None:
    """Swap the token and add the marker in one text part, keeping its transfer encoding."""
    content = part.get_content().replace(old_token, new_token)
    if marker:
        if part.get_content_subtype() == "html":
            paragraph = f'<p style="font-size:10px;color:#999999">{html_lib.escape(marker)}</p>'
            content, replaced = _BODY_END.subn(lambda found: paragraph + found.group(0), content, count=1)
            content = content if replaced else content + paragraph
        else:
            content = content.rstrip("\r\n") + f"\n\n{marker}\n"
    encoding = str(part.get("Content-Transfer-Encoding", "quoted-printable")).strip().lower()
    part.set_content(content, subtype=part.get_content_subtype(), charset="utf-8", cte=encoding)
    del part["MIME-Version"]  # set_content adds it; only the top-level message carries one


def _link_with(message: EmailMessage, token: str) -> str | None:
    """Return the URL carrying ``token``, preferring the plain-text part."""
    for subtype in ("plain", "html"):
        for part in message.walk():
            if part.get_content_type() == f"text/{subtype}":
                for url in _URL.findall(part.get_content()):
                    if token in url:
                        return html_lib.unescape(url)
    return None


@dataclass(frozen=True, kw_only=True)
class Marker:
    """Test and run identifiers stamped on generated mail (design §4).

    The test ID is always stored as a TestRail case ID (``C400001``; ``400001`` is accepted and converted).
    """

    test_id: str = ""
    run_id: str = ""

    def __post_init__(self) -> None:
        """Validate both identifiers when present; bring the test ID to TestRail form.

        Raises:
            InvalidInputError: ``CASE_CONDITION_INVALID_INPUT`` for a test ID that is not ``C<6+ digits>``
                or a run ID outside ASCII ``[A-Za-z0-9_-]``.
        """
        if self.test_id:
            case_id = testrail_case_id(self.test_id)
            if case_id is None:
                raise _invalid(f"test_id must be a TestRail case ID C<6+ digits> (e.g. C400001), got {self.test_id!r}",
                               field="test_id", value=self.test_id)
            object.__setattr__(self, "test_id", case_id)
        if self.run_id:
            validate_identifier("run_id", self.run_id)

    @property
    def line(self) -> str | None:
        """The body marker line, or ``None`` when no marker is set."""
        return _marker_line(self.test_id, self.run_id) if self.test_id or self.run_id else None


def compose(  # pylint: disable=too-many-arguments
    template: Template,
    address: str,
    *,
    field: Field = Field.TO,
    marker: Marker | None = None,
    sent_at: datetime,
    secret: bytes = TEST_ONLY_SECRET,
) -> ComposedEmail:
    """Build one email for one recipient.

    Args:
        template: What kind of email.
        address: The test address.
        field: Recipient field for the address.
        marker: Test/run marker (headers and body line).
        sent_at: The ``Date`` (timezone-aware).
        secret: Token signing key (test-only by default).

    Returns:
        The composed email.
    """
    address = validate_address(address)
    marker = marker or Marker()
    if template.kind is Kind.GENERIC:
        message, new_token, display_name = _from_text(template, marker), None, ""
    else:
        message, new_token, display_name = _from_sample(template, address, marker, sent_at, secret)
    _set_recipient(message, address, field, display_name)
    for header in ("Date", "Message-ID", "X-EMI-Test-Id", "X-EMI-Run-Id"):
        del message[header]
    message["Date"] = format_datetime(sent_at)
    message_id = make_msgid(domain="cloud.example.com")
    message["Message-ID"] = message_id
    if marker.test_id:
        message["X-EMI-Test-Id"] = marker.test_id
    if marker.run_id:
        message["X-EMI-Run-Id"] = marker.run_id
    return ComposedEmail(
        message=message, kind=template.kind, address=address, field=field,
        link=_link_with(message, new_token) if new_token else None,
        jti=str(token_claims(new_token)["jti"]) if new_token else None,
        message_id=message_id,
    )


def _from_text(template: Template, marker: Marker) -> EmailMessage:
    """Build a generic email (no links) from the template's subject and text."""
    message = EmailMessage()
    message["From"] = _FROM
    message["Subject"] = template.subject
    body = template.text.rstrip("\n") + (f"\n\n{marker.line}\n" if marker.line else "\n")
    message.set_content(body, charset="utf-8", cte="quoted-printable")
    return message


def _from_sample(
    template: Template, address: str, marker: Marker, sent_at: datetime, secret: bytes
) -> tuple[EmailMessage, str, str]:
    """Copy a sample email, give it a fresh token for ``address`` and add the marker.

    Returns:
        The message, its new token, and the sample's ``To`` display name.

    Raises:
        InvalidInputError: If the template has no sample or the sample has no token.
    """
    if template.source_eml is None:
        raise _invalid(f"template {template.name} has no source_eml", template=template.name)
    message = BytesParser(policy=policy.default).parsebytes(template.source_eml.read_bytes())
    display_name = message["To"].addresses[0].display_name if message["To"] else ""
    texts = [part.get_content() for part in message.walk() if part.get_content_maintype() == "text"]
    old_token = next((token for token in map(find_token, texts) if token), None)
    if old_token is None:
        raise _invalid(f"sample {template.source_eml.name} carries no token", template=template.name)
    new_token = reissue(old_token, address, sent_at, secret)
    for part in list(message.walk()):
        if part.get_content_maintype() == "text":
            _rewrite_part(part, old_token, new_token, marker.line)
    return message, new_token, display_name

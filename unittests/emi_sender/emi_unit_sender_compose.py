# SPDX-FileCopyrightText: 2026 Aleksandr Polskiy
# SPDX-License-Identifier: Apache-2.0
"""Unit tests: realistic email composition and tokens (design §6.1, §6.2)."""

from datetime import UTC, datetime, timedelta
from email import policy
from email.parser import BytesParser

import pytest

from emi_sender import Field, InvalidInputError, Kind, Marker, builtin_templates, compose
from emi_sender.delivery import serialize
from emi_sender.tokens import find_token, token_claims, verify

_SENT_AT = datetime(2026, 10, 26, 15, 30, 12, tzinfo=UTC)
_ADDRESS = "qa+C400001_20261026t153012-w01-a1b2@example.com"
_MARKER = Marker(test_id="400001", run_id="20261026t153012-w01-a1b2")


def _parts(raw: bytes) -> dict[str, list[tuple[str, str]]]:
    """Map content type to (transfer encoding, decoded content) of each part."""
    message = BytesParser(policy=policy.default).parsebytes(raw)
    result: dict[str, list[tuple[str, str]]] = {}
    for part in message.walk():
        if not part.is_multipart():
            content = part.get_content()
            text = content if isinstance(content, str) else "<binary>"
            result.setdefault(part.get_content_type(), []).append((str(part.get("Content-Transfer-Encoding")), text))
    return result


class TestEMISenderCompose:
    """Emails stay as realistic as the samples."""

    def EMI_SENDER_UNIT_110001_activation_token_is_reissued_for_recipient(self) -> None:
        """New HS256 token: sub = recipient, exp = Date + 24 h, a jti, valid signature."""
        composed = compose(builtin_templates()[Kind.ACTIVATION], _ADDRESS, marker=_MARKER, sent_at=_SENT_AT)
        assert composed.link is not None and "/activate?code=" in composed.link
        token = find_token(composed.link)
        assert token is not None and verify(token)
        claims = token_claims(token)
        assert claims["sub"] == _ADDRESS
        assert claims["exp"] == int((_SENT_AT + timedelta(hours=24)).timestamp())
        assert claims["jti"] == composed.jti

    def EMI_SENDER_UNIT_110002_encodings_and_structure_preserved(self) -> None:
        """Activation stays quoted-printable; invitation stays base64 with its inline logo."""
        activation = _parts(serialize(compose(builtin_templates()[Kind.ACTIVATION], _ADDRESS, sent_at=_SENT_AT)))
        assert {encoding for encoding, _text in activation["text/plain"] + activation["text/html"]} == {
            "quoted-printable"}
        invitation = _parts(serialize(compose(builtin_templates()[Kind.INVITATION], _ADDRESS, sent_at=_SENT_AT)))
        assert {encoding for encoding, _text in invitation["text/plain"] + invitation["text/html"]} == {"base64"}
        assert "image/png" in invitation

    def EMI_SENDER_UNIT_110003_old_token_gone_and_both_parts_agree(self) -> None:
        """The sample's token is replaced everywhere; HTML and text carry the same new token."""
        sample = (builtin_templates()[Kind.ACTIVATION].source_eml or "").read_text(encoding="utf-8")
        composed = compose(builtin_templates()[Kind.ACTIVATION], _ADDRESS, sent_at=_SENT_AT)
        parts = _parts(serialize(composed))
        new_token = find_token(composed.link or "")
        for _encoding, text in parts["text/plain"] + parts["text/html"]:
            assert new_token in text
            assert "eyJzdWIiOiJxYSs1ZTBjN2EzMUBleGFtcGxlLmNvbSIs" not in text
        assert "eyJzdWIiOiJxYSs1ZTBjN2EzMUBleGFtcGxlLmNvbSIs" in sample.replace("=\n", "").replace("=\r\n", "")

    def EMI_SENDER_UNIT_110004_invitation_keeps_site_claim_and_display_name(self) -> None:
        """Claims the sample has (site) are kept; the To display name is kept."""
        composed = compose(builtin_templates()[Kind.INVITATION], _ADDRESS, sent_at=_SENT_AT)
        claims = token_claims(find_token(composed.link or "") or "")
        assert claims["site"] == "6f1c2a9e-3b7d-4e0a-9c55-0d8e4b1f7a21"
        assert "exp" not in claims
        assert composed.message["To"].addresses[0].display_name == "Anna Schmidt"

    def EMI_SENDER_UNIT_110005_marker_in_headers_and_both_bodies(self) -> None:
        """X-EMI headers and the marker line at the bottom of each part, test ID as the TestRail case ID."""
        composed = compose(builtin_templates()[Kind.ACTIVATION], _ADDRESS, marker=_MARKER, sent_at=_SENT_AT)
        assert composed.message["X-EMI-Test-Id"] == "C400001"
        assert composed.message["X-EMI-Run-Id"] == "20261026t153012-w01-a1b2"
        parts = _parts(serialize(composed))
        assert parts["text/plain"][0][1].rstrip().endswith("EMI-Test: C400001 run=20261026t153012-w01-a1b2")
        html_text = parts["text/html"][0][1]
        assert html_text.index("EMI-Test: C400001") > html_text.index("Activate account")

    @pytest.mark.parametrize(("field", "to_header", "cc_header", "delivered_to"), [
        (Field.TO, _ADDRESS, None, None),
        (Field.CC, "undisclosed-recipients:;", _ADDRESS, None),
        (Field.BCC, "undisclosed-recipients:;", None, _ADDRESS),
    ])
    def EMI_SENDER_UNIT_110006_recipient_field_placement(
        self, field: Field, to_header: str, cc_header: str | None, delivered_to: str | None
    ) -> None:
        """To, Cc, or Bcc (no Bcc header; Delivered-To emulates Gmail)."""
        message = compose(builtin_templates()[Kind.ACTIVATION], _ADDRESS, field=field, sent_at=_SENT_AT).message
        assert to_header in str(message["To"])
        assert message["Cc"] == cc_header
        assert message["Bcc"] is None
        assert message["Delivered-To"] == delivered_to

    def EMI_SENDER_UNIT_110007_generic_has_no_links(self) -> None:
        """Generic mail is built from scratch and contains no link."""
        composed = compose(builtin_templates()[Kind.GENERIC], _ADDRESS, marker=_MARKER, sent_at=_SENT_AT)
        text = composed.message.get_content()
        assert "http" not in text and composed.link is None and composed.jti is None
        assert "EMI-Test: C400001" in text

    def EMI_SENDER_UNIT_110008_unique_message_ids_and_tokens(self) -> None:
        """Two emails of the same kind to the same address in the same second stay distinct."""
        template = builtin_templates()[Kind.ACTIVATION]
        first = compose(template, _ADDRESS, sent_at=_SENT_AT)
        second = compose(template, _ADDRESS, sent_at=_SENT_AT)
        assert first.message_id != second.message_id
        assert first.link != second.link

    @pytest.mark.parametrize("bad_marker", [{"test_id": "тест"}, {"run_id": "run id"}, {"test_id": "a/b"},
                                            {"test_id": "C40000"}, {"test_id": "T400001"}, {"test_id": "С400001"}])
    def EMI_SENDER_UNIT_110009_non_ascii_identifiers_rejected(self, bad_marker: dict[str, str]) -> None:
        """Run IDs outside [A-Za-z0-9_-] and test IDs that are not C + 6 digits (Cyrillic С too) are rejected."""
        with pytest.raises(InvalidInputError):
            Marker(**bad_marker)

    @pytest.mark.parametrize("test_id", ["400001", "C400001", "c400001"])
    def EMI_SENDER_UNIT_110065_test_id_in_testrail_form(self, test_id: str) -> None:
        """Every accepted test ID form is stored and sent as the TestRail case ID."""
        assert Marker(test_id=test_id).test_id == "C400001"

    @pytest.mark.parametrize("bad_address", ["qa+тест@example.com", "no-at-sign", "q" * 65 + "@example.com"])
    def EMI_SENDER_UNIT_110010_bad_addresses_rejected(self, bad_address: str) -> None:
        """Non-ASCII, malformed, or local part over 64 characters."""
        with pytest.raises(InvalidInputError):
            compose(builtin_templates()[Kind.ACTIVATION], bad_address, sent_at=_SENT_AT)

    def EMI_SENDER_UNIT_110011_serialized_with_crlf_only(self) -> None:
        """Every line ends in CRLF, so SMTP dot-stuffing cannot corrupt links (Spike Evidence)."""
        raw = serialize(compose(builtin_templates()[Kind.ACTIVATION], _ADDRESS, sent_at=_SENT_AT))
        assert b"\n" not in raw.replace(b"\r\n", b"")

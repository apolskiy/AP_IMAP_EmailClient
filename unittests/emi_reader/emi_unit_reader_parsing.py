# SPDX-FileCopyrightText: 2026 Aleksandr Polskiy
# SPDX-License-Identifier: Apache-2.0
"""Unit tests: settings, MIME decoding, CTA link extraction and token helpers."""

import pytest

from emi_reader import (
    Cta,
    ImapSettings,
    LinkExtractionError,
    ReaderCaseKeyword,
    ReaderKeyword,
    Security,
    TokenError,
    assert_link_for,
    extract_link,
    parse_email,
    token_payload,
    token_subject,
)
from unittests.emi_reader.reader_sims import FIXTURES, make_email


class TestEMIReaderSettings:
    """Connection settings: defaults, validation and redaction."""

    def EMI_READER_UNIT_100001_default_port_follows_security(self) -> None:
        """Each security mode has its own default port; an explicit port wins."""
        assert ImapSettings().effective_port == 3143
        assert ImapSettings(security=Security.SSL).effective_port == 3993
        assert ImapSettings(security=Security.SSL, port=993).effective_port == 993

    @pytest.mark.parametrize("bad_kwargs", [{"port": 0}, {"port": 65536}, {"connect_timeout": 0.5},
                                            {"read_timeout": 4000.0}, {"host": " "}, {"folders": ()}])
    def EMI_READER_UNIT_100002_out_of_range_values_are_rejected(self, bad_kwargs: dict[str, object]) -> None:
        """Out-of-range input is rejected when the settings are built."""
        with pytest.raises(ValueError):
            ImapSettings(**bad_kwargs)

    def EMI_READER_UNIT_100003_password_never_shown(self) -> None:
        """The password appears neither in repr nor in the report description."""
        settings = ImapSettings(password="s3cret-Value")
        assert "s3cret-Value" not in repr(settings)
        assert "s3cret-Value" not in str(settings.describe())
        assert settings.describe()["password"] == "***"


class TestEMIReaderMime:
    """Decoding the real sample emails."""

    def EMI_READER_UNIT_100010_decodes_activation_sample(self) -> None:
        """Quoted-printable multipart/alternative: both parts decoded, recipient found."""
        email = parse_email((FIXTURES / "activation.eml").read_bytes())
        assert email.html is not None and "Activate account" in email.html
        assert email.text is not None and "/activate?code=" in email.text
        assert email.addresses_in(("to",)) == {"qa+5e0c7a31@example.com"}

    def EMI_READER_UNIT_100011_decodes_invitation_sample(self) -> None:
        """Base64 parts inside multipart/related; display name stripped from the address."""
        email = parse_email((FIXTURES / "site_invitation.eml").read_bytes())
        assert email.html is not None and "Accept invitation" in email.html
        assert email.addresses_in(("TO",)) == {"qa+9b24d6f0@example.com"}

    def EMI_READER_UNIT_100012_reads_markers_and_fields(self) -> None:
        """Marker headers and each recipient field are exposed separately."""
        raw = make_email(to_addr="a@example.com", cc_addr="B@Example.com", test_id="400001", run_id="r1")
        email = parse_email(raw)
        assert (email.test_id, email.run_id) == ("400001", "r1")
        assert email.addresses_in(("cc",)) == {"b@example.com"}
        assert email.addresses_in(("bcc",)) == set()


class TestEMIReaderLinks:
    """CTA link extraction rules (design §5.6)."""

    def EMI_READER_UNIT_100020_extracts_activation_link_from_sample(self) -> None:
        """The button link is found in HTML and agrees with the text part."""
        result = extract_link(parse_email((FIXTURES / "activation.eml").read_bytes()), Cta.ACTIVATE_ACCOUNT)
        assert result.url.startswith("https://cloud.example.com/activate?code=")
        assert result.url.endswith("&lang=en")
        assert result.source == "html"

    def EMI_READER_UNIT_100021_extracts_invitation_link_from_sample(self) -> None:
        """The invitation button is found; the site link and footer links are ignored."""
        result = extract_link(parse_email((FIXTURES / "site_invitation.eml").read_bytes()),
                              Cta.ACCEPT_INVITATION)
        assert "/invite/accept?token=" in result.url

    def EMI_READER_UNIT_100022_wrong_cta_is_not_found(self) -> None:
        """Asking an activation email for the invitation button fails with LINK_NOT_FOUND."""
        email = parse_email((FIXTURES / "activation.eml").read_bytes())
        with pytest.raises(LinkExtractionError) as caught:
            extract_link(email, Cta.ACCEPT_INVITATION)
        assert caught.value.primary == ReaderCaseKeyword.LINK_NOT_FOUND

    def EMI_READER_UNIT_100023_plain_text_only_uses_fallback(self) -> None:
        """No HTML part: the text rule finds the link and the secondary keyword is recorded."""
        email = parse_email(make_email(to_addr="a@example.com", html=False))
        result = extract_link(email, Cta.ACTIVATE_ACCOUNT)
        assert result.source == "text"
        assert ReaderKeyword.PARSE_NO_HTML_PART in result.secondary

    def EMI_READER_UNIT_100024_html_text_mismatch_fails(self) -> None:
        """Different HTML and text links fail by default (possible phishing)."""
        email = parse_email(make_email(to_addr="a@example.com", text_path="/activate", path="/activate/"))
        with pytest.raises(LinkExtractionError) as caught:
            extract_link(email, Cta.ACTIVATE_ACCOUNT)
        assert caught.value.primary == ReaderCaseKeyword.LINK_HTML_TEXT_MISMATCH

    def EMI_READER_UNIT_100025_html_text_mismatch_can_warn(self) -> None:
        """In warn mode the HTML link is returned with a warning and a secondary keyword."""
        email = parse_email(make_email(to_addr="a@example.com", text_path="/activate", path="/activate/"))
        result = extract_link(email, Cta.ACTIVATE_ACCOUNT, mismatch_is_error=False)
        assert result.warnings and ReaderCaseKeyword.LINK_HTML_TEXT_MISMATCH in result.secondary

    def EMI_READER_UNIT_100026_label_match_is_exact_after_normalization(self) -> None:
        """Case and whitespace are normalized, but a longer label does not match."""
        assert extract_link(parse_email(make_email(to_addr="a@x.com", label="  ACTIVATE \n account ")),
                            Cta.ACTIVATE_ACCOUNT).source == "html"
        result = extract_link(parse_email(make_email(to_addr="a@x.com", label="Activate account now")),
                              Cta.ACTIVATE_ACCOUNT)
        assert result.source == "text"

    def EMI_READER_UNIT_100027_two_different_buttons_are_ambiguous(self) -> None:
        """Two distinct links behind the same label fail instead of guessing."""
        html_body = ('<a class="btn" href="https://x.com/activate?a=1">Activate account</a>'
                     '<a class="btn" href="https://x.com/activate?a=2">Activate account</a>')
        email = parse_email(f"To: a@x.com\nContent-Type: text/html\n\n{html_body}".encode())
        with pytest.raises(LinkExtractionError) as caught:
            extract_link(email, Cta.ACTIVATE_ACCOUNT)
        assert caught.value.primary == ReaderCaseKeyword.LINK_AMBIGUOUS

    def EMI_READER_UNIT_100028_custom_cta_needs_no_code_change(self) -> None:
        """A new CTA kind (password reset) works through the same extractor."""
        reset = Cta(label="Reset password", url_pattern=r"^/reset$")
        email = parse_email(make_email(to_addr="a@x.com", label="Reset password", path="/reset"))
        assert "/reset?code=" in extract_link(email, reset).url


class TestEMIReaderTokens:
    """Token helpers (design §5.7)."""

    def EMI_READER_UNIT_100030_sample_token_subject_is_recipient(self) -> None:
        """The sample's token was issued to the sample's recipient."""
        link = extract_link(parse_email((FIXTURES / "activation.eml").read_bytes()), Cta.ACTIVATE_ACCOUNT).url
        assert token_subject(link) == "qa+5e0c7a31@example.com"
        assert "exp" in token_payload(link)
        assert_link_for(link, "QA+5E0C7A31@example.com")

    def EMI_READER_UNIT_100031_foreign_token_is_recipient_mismatch(self) -> None:
        """A link issued to another address fails with LINK_RECIPIENT_MISMATCH."""
        link = extract_link(parse_email((FIXTURES / "site_invitation.eml").read_bytes()),
                            Cta.ACCEPT_INVITATION).url
        with pytest.raises(TokenError) as caught:
            assert_link_for(link, "qa+someone_else@example.com")
        assert caught.value.primary == ReaderCaseKeyword.LINK_RECIPIENT_MISMATCH

    def EMI_READER_UNIT_100032_link_without_token_fails(self) -> None:
        """A link with no JWT cannot be attributed to anyone."""
        with pytest.raises(TokenError):
            token_subject("https://cloud.example.com/activate?code=plain")

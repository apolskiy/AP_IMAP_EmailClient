# SPDX-FileCopyrightText: 2026 Aleksandr Polskiy
# SPDX-License-Identifier: Apache-2.0
"""Unit tests: folder names in modified UTF-7 (RFC 3501 §5.1.3, design §5.1)."""

import pytest

from emi_reader.folders import decode_folder, encode_folder
from emi_reader.transport import quote_folder


class TestEMIReaderFolderNames:
    """Unicode names in the API, modified UTF-7 on the wire."""

    @pytest.mark.parametrize(("name", "wire"), [
        ("INBOX", "INBOX"),
        ("~peter/mail/台北/日本語", "~peter/mail/&U,BTFw-/&ZeVnLIqe-"),  # RFC 3501 §5.1.3 example
        ("Входящие", "&BBIERQQ+BDQETwRJBDgENQ-"),
        ("Tom & Jerry", "Tom &- Jerry"),
        ("Spam ✉ Junk", "Spam &Jwk- Junk"),
    ])
    def EMI_READER_UNIT_100110_encodes_and_decodes(self, name: str, wire: str) -> None:
        """Known encodings, both directions."""
        assert encode_folder(name) == wire
        assert decode_folder(wire) == name

    @pytest.mark.parametrize("name", ["Входящие/Архив", "😀 emoji", "mix Ünïcödé & ASCII", "&&--", ""])
    def EMI_READER_UNIT_100111_round_trips(self, name: str) -> None:
        """Every name survives encode then decode (surrogate pairs included); the wire form is ASCII."""
        wire = encode_folder(name)
        assert wire.isascii() and decode_folder(wire) == name

    def EMI_READER_UNIT_100112_malformed_sequence_kept(self) -> None:
        """A broken shift sequence from the server is kept as received instead of failing the wait."""
        assert decode_folder("Bad &!!!- name") == "Bad &!!!- name"
        assert decode_folder("open & never closed") == "open & never closed"

    def EMI_READER_UNIT_100113_quoted_for_commands(self) -> None:
        """Commands get the encoded name in quotes, with quotes and backslashes escaped."""
        assert quote_folder("Входящие") == '"&BBIERQQ+BDQETwRJBDgENQ-"'
        assert quote_folder('a"b\\c') == '"a\\"b\\\\c"'

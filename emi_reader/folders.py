# SPDX-FileCopyrightText: 2026 Aleksandr Polskiy
# SPDX-License-Identifier: Apache-2.0
"""Folder names on the wire: modified UTF-7 (RFC 3501 §5.1.3, design §5.1).

Our API and settings use ordinary Unicode names (``Входящие``); IMAP servers expect them in modified
UTF-7 (``&BBIERQQ+BDQETwRJBDgENQ-``). Printable ASCII stays as is, ``&`` becomes ``&-``, and every run of
other characters becomes ``&`` + base64 of its UTF-16BE bytes (``,`` instead of ``/``, no padding) + ``-``.
Plain UTF-8 names (``UTF8=ACCEPT``, RFC 6855) are not used: Greenmail does not support them (design §15).
"""

import base64
import binascii


def _is_direct(char: str) -> bool:
    """Printable US-ASCII characters are sent as themselves."""
    return 0x20 <= ord(char) <= 0x7E


def encode_folder(name: str) -> str:
    """Encode a folder name for the wire.

    Args:
        name: Unicode folder name.

    Returns:
        The modified UTF-7 form; ASCII names without ``&`` are returned unchanged.
    """
    encoded: list[str] = []
    pending: list[str] = []

    def _flush() -> None:
        if pending:
            raw = "".join(pending).encode("utf-16-be")
            encoded.append("&" + base64.b64encode(raw).decode("ascii").rstrip("=").replace("/", ",") + "-")
            pending.clear()

    for char in name:
        if _is_direct(char):
            _flush()
            encoded.append("&-" if char == "&" else char)
        else:
            pending.append(char)
    _flush()
    return "".join(encoded)


def decode_folder(wire_name: str) -> str:
    """Decode a folder name received from the server.

    Args:
        wire_name: The name as ``LIST`` returns it.

    Returns:
        The Unicode name. A malformed shift sequence is kept as received rather than failing the wait.
    """
    decoded: list[str] = []
    position = 0
    while position < len(wire_name):
        char = wire_name[position]
        end = wire_name.find("-", position + 1) if char == "&" else -1
        if char != "&" or end == -1:
            decoded.append(char)
            position += 1
            continue
        chunk = wire_name[position + 1:end]
        decoded.append("&" if not chunk else _decode_chunk(chunk, wire_name[position:end + 1]))
        position = end + 1
    return "".join(decoded)


def _decode_chunk(chunk: str, original: str) -> str:
    """Decode one ``&...-`` sequence; return it unchanged when it is malformed."""
    padded = chunk.replace(",", "/") + "=" * (-len(chunk) % 4)
    try:
        return base64.b64decode(padded, validate=True).decode("utf-16-be")
    except (binascii.Error, UnicodeDecodeError):
        return original

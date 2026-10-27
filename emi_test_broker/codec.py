# SPDX-FileCopyrightText: 2026 Aleksandr Polskiy
# SPDX-License-Identifier: Apache-2.0
"""JSON shapes exchanged between the broker and its clients.

Errors cross the wire with their class name and keywords and are rebuilt as the same typed error on the
client, so a test sees ``CASE_EMAIL_NOT_RECEIVED`` the same way in ``service`` and ``inprocess`` mode.
"""

import base64
from dataclasses import asdict, fields
from typing import Any, TypeVar

from emi_greenmail.errors import GreenmailError
from emi_reader import Cta, ParsedEmail, RawEmail, ReaderError, ReceivedEmail
from emi_sender import BatchRequest, Field, Manifest, ManifestEntry, Marker, Order, Preset, SenderError
from emi_test_broker.errors import RemoteError, TestBrokerError

_BASE = TypeVar("_BASE", bound=Exception)


def _family(base: type[_BASE]) -> dict[str, type[_BASE]]:
    """Every class of an error family by name: the base and all its subclasses."""
    found: dict[str, type[_BASE]] = {base.__name__: base}
    pending = list(base.__subclasses__())
    while pending:
        subclass = pending.pop()
        found[subclass.__name__] = subclass
        pending.extend(subclass.__subclasses__())
    return found


_READER_ERRORS = _family(ReaderError)
_SENDER_ERRORS = _family(SenderError)
_BROKER_ERRORS = _family(TestBrokerError)
_GREENMAIL_ERRORS = _family(GreenmailError)


def error_to_json(error: Exception) -> dict[str, Any]:
    """Describe an error for the wire.

    Args:
        error: Any error raised while serving a request.

    Returns:
        Class name, message, keywords and diagnostics.
    """
    return {
        "type": type(error).__name__,
        "message": str(error),
        "primary": str(getattr(error, "primary", "")),
        "secondary": [str(item) for item in getattr(error, "secondary", ())],
        "retryable": bool(getattr(error, "retryable", False)),
        "infrastructure": bool(getattr(error, "infrastructure", False)),
        "details": _plain(getattr(error, "details", {})),
    }


def error_from_json(data: dict[str, Any]) -> Exception:
    """Rebuild a typed error from its wire form.

    Args:
        data: Output of :func:`error_to_json`.

    Returns:
        An instance of the original class when it is one of ours, otherwise :class:`RemoteError`.
    """
    name = str(data.get("type", ""))
    message = str(data.get("message", ""))
    primary = str(data.get("primary", ""))
    secondary = tuple(data.get("secondary", ()))
    details = dict(data.get("details", {}))
    infrastructure = bool(data.get("infrastructure", False))
    retryable = bool(data.get("retryable", False))
    if name == "ValueError":
        return ValueError(message)
    if (reader_class := _READER_ERRORS.get(name)) is not None:
        return reader_class(message, primary, secondary=secondary, retryable=retryable,
                            infrastructure=infrastructure, details=details)
    if (sender_class := _SENDER_ERRORS.get(name)) is not None:
        return sender_class(message, primary, retryable=retryable, infrastructure=infrastructure, details=details)
    if (broker_class := _BROKER_ERRORS.get(name)) is not None:
        return broker_class(message, primary, secondary=secondary, infrastructure=infrastructure, details=details)
    if (greenmail_class := _GREENMAIL_ERRORS.get(name)) is not None:
        return greenmail_class(message, primary, secondary=secondary, infrastructure=infrastructure,
                               details=details)
    return RemoteError(f"{name}: {message}", primary, secondary=secondary, infrastructure=infrastructure,
                       details=details)


def cta_to_json(cta: Cta | None) -> dict[str, str] | None:
    """Describe a CTA.

    Args:
        cta: The button, or None.

    Returns:
        Label and URL pattern, or None.
    """
    return None if cta is None else {"label": cta.label, "url_pattern": cta.url_pattern}


def cta_from_json(data: dict[str, Any] | None) -> Cta | None:
    """Rebuild a CTA.

    Args:
        data: Output of :func:`cta_to_json`.

    Returns:
        The button, or None.
    """
    return None if data is None else Cta(label=str(data["label"]), url_pattern=str(data["url_pattern"]))


def received_to_json(item: ReceivedEmail) -> dict[str, Any]:
    """Describe a received email.

    Args:
        item: The email.

    Returns:
        Every field, bodies included.
    """
    email = item.email
    return {"uid": item.uid, "folder": item.folder, "link": item.link, "secondary": list(item.secondary),
            "email": {"message_id": email.message_id, "subject": email.subject,
                      "recipients": {key: list(value) for key, value in email.recipients.items()},
                      "html": email.html, "text": email.text, "test_id": email.test_id, "run_id": email.run_id}}


def received_from_json(data: dict[str, Any]) -> ReceivedEmail:
    """Rebuild a received email.

    Args:
        data: Output of :func:`received_to_json`.

    Returns:
        The email.
    """
    email = data["email"]
    parsed = ParsedEmail(message_id=email["message_id"], subject=email["subject"],
                         recipients={key: tuple(value) for key, value in email["recipients"].items()},
                         html=email["html"], text=email["text"], test_id=email["test_id"], run_id=email["run_id"])
    return ReceivedEmail(uid=int(data["uid"]), folder=str(data["folder"]), email=parsed, link=data["link"],
                         secondary=tuple(data["secondary"]))


def raw_email_to_json(item: RawEmail) -> dict[str, Any]:
    """Describe a stored email; the bytes travel base64-encoded.

    Args:
        item: The email.

    Returns:
        Folder, UID, address, subject and ``raw_base64``.
    """
    return {"uid": item.uid, "folder": item.folder, "address": item.address, "subject": item.subject,
            "raw_base64": base64.b64encode(item.raw).decode("ascii")}


def raw_email_from_json(data: dict[str, Any]) -> RawEmail:
    """Rebuild a stored email.

    Args:
        data: Output of :func:`raw_email_to_json`.

    Returns:
        The email.
    """
    return RawEmail(uid=int(data["uid"]), folder=str(data["folder"]), address=str(data["address"]),
                    subject=str(data["subject"]), raw=base64.b64decode(data["raw_base64"]))


def manifest_to_json(manifest: Manifest) -> dict[str, Any]:
    """Describe a manifest.

    Args:
        manifest: The manifest.

    Returns:
        Entries and batch facts.
    """
    return {"entries": [asdict(entry) for entry in manifest.entries], "seed": manifest.seed,
            "test_id": manifest.test_id, "run_id": manifest.run_id, "settings": manifest.settings,
            "clamped": list(manifest.clamped)}


def manifest_from_json(data: dict[str, Any]) -> Manifest:
    """Rebuild a manifest.

    Args:
        data: Output of :func:`manifest_to_json`.

    Returns:
        The manifest.
    """
    known = {item.name for item in fields(ManifestEntry)}
    entries = [ManifestEntry(**{key: value for key, value in entry.items() if key in known})
               for entry in data.get("entries", [])]
    return Manifest(entries=entries, seed=int(data.get("seed", 0)), test_id=str(data.get("test_id", "")),
                    run_id=str(data.get("run_id", "")), settings=dict(data.get("settings", {})),
                    clamped=tuple(data.get("clamped", ())))


def batch_request_to_json(request: BatchRequest) -> dict[str, Any]:
    """Describe a batch request.

    Args:
        request: The request.

    Returns:
        Its fields as plain values.
    """
    return {"addresses": list(request.addresses), "counts": dict(request.counts), "preset": str(request.preset),
            "explicit": {key: dict(value) for key, value in request.explicit.items()} if request.explicit else None,
            "order": str(request.order), "seed": request.seed, "recipient_field": str(request.recipient_field),
            "concurrency": request.concurrency, "jitter_ms": request.jitter_ms}


def batch_request_from_json(data: dict[str, Any]) -> BatchRequest:
    """Rebuild a batch request; validation happens when it is planned.

    Args:
        data: Output of :func:`batch_request_to_json`.

    Returns:
        The request.

    Raises:
        ValueError: If an enumerated value is unknown.
    """
    return BatchRequest(addresses=tuple(data.get("addresses", ())), counts=dict(data.get("counts", {})),
                        preset=Preset(data.get("preset", Preset.ALL_TO_ONE)), explicit=data.get("explicit"),
                        order=Order(data.get("order", Order.FIXED)), seed=data.get("seed"),
                        recipient_field=Field(data.get("recipient_field", Field.TO)),
                        concurrency=data.get("concurrency", 1), jitter_ms=data.get("jitter_ms", 0))


def marker_from_json(data: dict[str, Any]) -> Marker:
    """Rebuild a marker (validated by :class:`Marker`).

    Args:
        data: ``{"test_id": ..., "run_id": ...}``.

    Returns:
        The marker.
    """
    return Marker(test_id=str(data.get("test_id") or ""), run_id=str(data.get("run_id") or ""))


def _plain(value: Any) -> Any:
    """Make diagnostics JSON-safe."""
    if isinstance(value, dict):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_plain(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)

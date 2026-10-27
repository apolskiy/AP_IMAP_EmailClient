# SPDX-FileCopyrightText: 2026 Aleksandr Polskiy
# SPDX-License-Identifier: Apache-2.0
"""Unit tests: the named faults break exactly what they claim and pass everything else through (design §13.4)."""

from typing import Any

import pytest

from emi_harness.faults import FAULT_CHOICES, FAULTS, NO_FAULT, FaultyBackend, apply_fault
from emi_reader import Cta, ReceivedEmail
from emi_sender import BatchRequest, Field, Manifest, ManifestEntry, Marker

_ADDRESS = "qa+C400001_20261026t153012-w00-a1b2@example.com"
_SLOT_ADDRESS = "qa+C400001_20261026t153012-w00-a1b2-r03@example.com"
_MARKER = Marker(test_id="C400001", run_id="20261026t153012-w00-a1b2")


class SimBackend:
    """Records every call and answers with predictable manifests, links and emails."""

    def __init__(self) -> None:
        """Start with no calls."""
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def health(self) -> dict[str, Any]:
        """A call no fault overrides."""
        self.calls.append(("health", {}))
        return {"status": "ok"}

    def send(self, kind: str, address: str, count: int = 1, *, marker: Marker | None = None) -> Manifest:
        """``count`` sent entries for the address."""
        self.calls.append(("send", {"kind": kind, "address": address, "count": count, "marker": marker}))
        return Manifest(entries=[ManifestEntry(index=index, address=address, field="to", kind=kind, status="sent",
                                               link=f"https://app.example/{address}/{index}0",
                                               message_id=f"<{address}-{index}>") for index in range(count)])

    def send_batch(self, request: BatchRequest, *, marker: Marker | None = None) -> Manifest:
        """Record the request; every asked email as sent to the first address."""
        self.calls.append(("send_batch", {"request": request, "marker": marker}))
        kinds = [kind for kind, count in request.counts.items() for _copy in range(int(str(count)))]
        return Manifest(entries=[ManifestEntry(index=index, address=request.addresses[0], field="to", kind=kind,
                                               status="sent") for index, kind in enumerate(kinds)])

    def wait_for_link(self, address: str, cta: Cta, **options: Any) -> str:
        """A link ending in 0."""
        self.calls.append(("wait_for_link", {"address": address, "cta": cta, **options}))
        return "https://app.example/activate?token=abc0"

    def wait_for_emails(self, address: str, **options: Any) -> list[ReceivedEmail]:
        """``count`` emails, UIDs 1..count, links ending in their UID and 0."""
        self.calls.append(("wait_for_emails", {"address": address, **options}))
        return [ReceivedEmail(uid=uid, folder="INBOX", email=None, link=f"https://app.example/{uid}0")  # type: ignore[arg-type]
                for uid in range(1, int(options.get("count", 1)) + 1)]


def _wait_options(**changes: Any) -> dict[str, Any]:
    """The options of one ``wait_for_emails`` call as the API passes them."""
    options: dict[str, Any] = {"cta": Cta.ACTIVATE_ACCOUNT, "count": 2, "timeout": 60.0, "test_id": "C400001",
                               "run_id": "run", "match_fields": ("cc",), "settle_window": 0.0}
    options.update(changes)
    return options


def _faulty(name: str) -> tuple[Any, SimBackend]:
    """The named fault around a fresh simulation."""
    sim = SimBackend()
    return apply_fault(name, sim), sim  # type: ignore[arg-type]


class TestEMIHarnessFaults:
    """Each fault breaks one behaviour; everything else reaches the real backend unchanged."""

    def EMI_HARNESS_UNIT_200130_catalog_and_selection(self) -> None:
        """'none' leaves the backend as it is; every catalog name wraps it; an unknown name is refused."""
        sim = SimBackend()
        assert apply_fault(NO_FAULT, sim) is sim  # type: ignore[arg-type]
        assert FAULT_CHOICES[0] == NO_FAULT and set(FAULT_CHOICES[1:]) == set(FAULTS) and len(FAULTS) == 9
        for name in FAULTS:
            assert isinstance(apply_fault(name, sim), FaultyBackend)  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="unknown fault"):
            apply_fault("reader-is-broken", sim)  # type: ignore[arg-type]

    def EMI_HARNESS_UNIT_200131_passes_everything_else_through(self) -> None:
        """A fault keeps the calls it does not break: same arguments, same answers, other attributes too."""
        faulty, sim = _faulty("reader-wrong-link")
        assert faulty.health() == {"status": "ok"}
        faulty.send("generic", _ADDRESS, 2, marker=_MARKER)
        faulty.wait_for_emails(_ADDRESS, **_wait_options(cta=None))
        assert sim.calls[1] == ("send", {"kind": "generic", "address": _ADDRESS, "count": 2, "marker": _MARKER})
        assert sim.calls[2] == ("wait_for_emails", {"address": _ADDRESS, **_wait_options(cta=None)})

    def EMI_HARNESS_UNIT_200132_sender_drops_email(self) -> None:
        """One email fewer reaches the address; the manifest still lists all of them at the address."""
        faulty, sim = _faulty("sender-drops-email")
        manifest = faulty.send("activation", _ADDRESS, 3, marker=_MARKER)
        delivered = {call[1]["address"]: call[1]["count"] for call in sim.calls}
        assert delivered == {_ADDRESS: 2, _ADDRESS.replace("@", "-dropped@"): 1}
        assert [entry.address for entry in manifest.entries] == [_ADDRESS] * 3
        assert [entry.index for entry in manifest.entries] == [0, 1, 2]
        single = faulty.send("activation", _ADDRESS, 1, marker=_MARKER)
        assert sim.calls[-1][1]["address"] != _ADDRESS and single.entries[0].address == _ADDRESS

    def EMI_HARNESS_UNIT_200139_sender_drops_email_in_batches(self) -> None:
        """A batch also loses one email to the sink; the manifest lists every asked email at the address."""
        faulty, sim = _faulty("sender-drops-email")
        request = BatchRequest(addresses=(_ADDRESS,), counts={"activation": 2, "generic": 1}, recipient_field=Field.CC)
        manifest = faulty.send_batch(request, marker=_MARKER)
        assert sim.calls[0][0] == "send" and sim.calls[0][1]["address"] == _ADDRESS.replace("@", "-dropped@")
        assert sim.calls[1][1]["request"].counts == {"activation": 1, "generic": 1}
        assert sim.calls[1][1]["request"].recipient_field is Field.CC
        assert [entry.address for entry in manifest.entries] == [_ADDRESS] * 3
        assert sorted(entry.kind for entry in manifest.entries) == ["activation", "activation", "generic"]
        faulty.send_batch(BatchRequest(addresses=(_ADDRESS,), counts={"invitation": 1}))
        assert [call[0] for call in sim.calls[2:]] == ["send"]

    @pytest.mark.parametrize("name", sorted(FAULTS))
    def EMI_HARNESS_UNIT_200129_probe_is_never_broken(self, name: str) -> None:
        """The session probe's calls reach the real backend unchanged, under every fault."""
        probe = "qa+C000000_20261026t153012-w00-a1b2@example.com"
        faulty, sim = _faulty(name)
        request = BatchRequest(addresses=(probe,), counts={"activation": 1, "generic": 1})
        assert len(faulty.send_batch(request, marker=_MARKER).entries) == 2
        faulty.send("activation", probe, 1)
        assert faulty.wait_for_link(probe, Cta.ACTIVATE_ACCOUNT) == "https://app.example/activate?token=abc0"
        faulty.wait_for_emails(probe.lower(), **_wait_options())
        assert [call[0] for call in sim.calls] == ["send_batch", "send", "wait_for_link", "wait_for_emails"]
        assert sim.calls[0][1]["request"] is request
        assert sim.calls[3] == ("wait_for_emails", {"address": probe.lower(), **_wait_options()})

    def EMI_HARNESS_UNIT_200133_sender_ignores_field(self) -> None:
        """The batch goes out with the address in To, whatever field was asked."""
        faulty, sim = _faulty("sender-ignores-field")
        faulty.send_batch(BatchRequest(addresses=(_ADDRESS,), counts={"activation": 1}, recipient_field=Field.CC))
        assert sim.calls[0][1]["request"].recipient_field is Field.TO

    @pytest.mark.parametrize(("name", "changed"), [
        ("reader-any-field", {"match_fields": ("to", "cc", "bcc", "delivered-to", "x-original-to")}),
        ("reader-ignores-markers", {"test_id": None, "run_id": None}),
        ("reader-gives-up-early", {"timeout": 1.0}),
        ("reader-slot-collapse", {}),
    ])
    def EMI_HARNESS_UNIT_200134_wait_arguments_changed(self, name: str, changed: dict[str, Any]) -> None:
        """Faults that change what the wait asks for change exactly those arguments."""
        faulty, sim = _faulty(name)
        faulty.wait_for_emails(_ADDRESS, **_wait_options())
        assert sim.calls[0] == ("wait_for_emails", {"address": _ADDRESS, **_wait_options(**changed)})

    def EMI_HARNESS_UNIT_200135_slot_collapse(self) -> None:
        """A slot address is read at slot 0; slot 0 itself is unchanged."""
        faulty, sim = _faulty("reader-slot-collapse")
        faulty.wait_for_emails(_SLOT_ADDRESS, **_wait_options())
        faulty.wait_for_emails(_ADDRESS, **_wait_options())
        assert [call[1]["address"] for call in sim.calls] == [_ADDRESS, _ADDRESS]

    def EMI_HARNESS_UNIT_200136_result_faults(self) -> None:
        """Duplicates keep the count with one email twice; extra adds one; a single email is left as it is."""
        duplicates, _sim = _faulty("reader-duplicates-email")
        extra, _other = _faulty("reader-returns-extra")
        assert [item.uid for item in duplicates.wait_for_emails(_ADDRESS, **_wait_options(count=3))] == [1, 2, 1]
        assert [item.uid for item in duplicates.wait_for_emails(_ADDRESS, **_wait_options(count=1))] == [1]
        assert [item.uid for item in extra.wait_for_emails(_ADDRESS, **_wait_options(count=2))] == [1, 2, 1]

    def EMI_HARNESS_UNIT_200137_wrong_link(self) -> None:
        """Every link returned differs from the real one in its last character."""
        faulty, _sim = _faulty("reader-wrong-link")
        assert faulty.wait_for_link(_ADDRESS, Cta.ACTIVATE_ACCOUNT) == "https://app.example/activate?token=abc1"
        links = [item.link for item in faulty.wait_for_emails(_ADDRESS, **_wait_options(count=2))]
        assert links == ["https://app.example/11", "https://app.example/21"]

    def EMI_HARNESS_UNIT_200138_gives_up_early_on_links(self) -> None:
        """A link wait is capped at 1 s too; a shorter timeout is kept."""
        faulty, sim = _faulty("reader-gives-up-early")
        faulty.wait_for_link(_ADDRESS, Cta.ACTIVATE_ACCOUNT, timeout=60.0)
        faulty.wait_for_link(_ADDRESS, Cta.ACTIVATE_ACCOUNT, timeout=0.5)
        assert [call[1]["timeout"] for call in sim.calls] == [1.0, 0.5]

# SPDX-FileCopyrightText: 2026 Aleksandr Polskiy
# SPDX-License-Identifier: Apache-2.0
"""Unit tests: per-test evidence and the job's settings file (design §9.8)."""

import json
from pathlib import Path

import pytest

from emi_harness import CaseFailure, Steps
from emi_harness.evidence import Evidence, safe_name, write_settings
from emi_harness.options import resolve
from emi_harness.results import load_results
from emi_reader import EmailWaitError, ReaderCaseKeyword

_RUN_ID = "20261026t153012-w00-a1b2"


class TestEMIHarnessEvidence:
    """Collecting, writing and showing evidence."""

    def EMI_HARNESS_UNIT_200120_collect_and_write(self, tmp_path: Path) -> None:
        """Items keep their order, become JSON files under attachments/<case>/, and report sections."""
        shown: list[tuple[str, str]] = []
        evidence = Evidence(lambda name, text: shown.append((name, text)))
        evidence.add("manifest", {"entries": [{"status": "sent"}], "path": Path("x")})
        evidence.add("note", ("one", 2))
        paths = evidence.write(tmp_path, "C400001")
        assert paths == ("attachments/C400001/01_manifest.json", "attachments/C400001/02_note.json")
        assert json.loads((tmp_path / paths[0]).read_text(encoding="utf-8"))["path"] == "x"
        assert [name for name, _text in shown] == ["manifest", "note"]
        assert evidence.sections()[1] == ("EMI evidence: note", json.dumps(["one", 2], indent=2))
        assert not Evidence().write(tmp_path, "C400002") and not (tmp_path / "attachments" / "C400002").exists()

    def EMI_HARNESS_UNIT_200121_error_details_through_causes(self) -> None:
        """The details of the tool error behind a step failure are kept, with its class and keyword."""
        steps = Steps()
        details = {"attempts": [{"keyword": "READER_IMAP_READ_TIMEOUT"}], "folders": [{"folder": "INBOX"}]}
        with pytest.raises(CaseFailure) as caught:
            with steps.step("wait"):
                raise EmailWaitError("none", ReaderCaseKeyword.EMAIL_NOT_RECEIVED, details=details)
        steps.evidence.add_error(caught.value)
        name, data = steps.evidence.items[0]
        assert name == "error_details" and data["error"] == "EmailWaitError"
        assert data["primary"] == ReaderCaseKeyword.EMAIL_NOT_RECEIVED and data["folders"] == [{"folder": "INBOX"}]
        plain = Evidence()
        plain.add_error(ValueError("no details"))
        assert not plain.items

    def EMI_HARNESS_UNIT_200122_safe_names(self) -> None:
        """Node IDs become usable folder names."""
        expected = "tests_emi_func_demo.py_TestEMIDemo_EMI_X_a_b"
        assert safe_name("tests/emi_func_demo.py::TestEMIDemo::EMI_X[a b]") == expected
        assert safe_name("::") == "item"

    def EMI_HARNESS_UNIT_200123_settings_file(self, tmp_path: Path) -> None:
        """The job's settings are written with their sources and without secrets."""
        resolved = resolve(environ={"EMI_IMAP_PASSWORD": "s3cret", "EMI_IMAP_HOST": "mail.test"})
        path = write_settings(tmp_path / "job", resolved.describe())
        text = path.read_text(encoding="utf-8")
        assert "s3cret" not in text
        assert json.loads(text)["imap_host"] == {"value": "mail.test", "source": "env", "env": "EMI_IMAP_HOST"}


class TestEMIHarnessEvidencePlugin:
    """Evidence through pytest (throwaway project)."""

    def EMI_HARNESS_UNIT_200124_attachments_in_results_and_report(self, pytester: pytest.Pytester,
                                                                    monkeypatch: pytest.MonkeyPatch) -> None:
        """steps.attach and a tool error's details become files, results entries and report sections."""
        monkeypatch.setenv("EMI_RUN_ID", _RUN_ID)
        monkeypatch.delenv("PYTEST_XDIST_WORKER", raising=False)
        pytester.makepyprojecttoml('[tool.pytest.ini_options]\npython_files = ["emi_*.py"]\n'
                                   'python_functions = ["EMI_*"]\n')
        pytester.makepyfile(**{"tests/emi_func_evidence": """
from emi_reader import EmailWaitError, ReaderCaseKeyword


def EMI_DEMO_FUNC_400061_fails_with_details(steps) -> None:
    steps.attach("plan", {"emails": 1})
    with steps.step("wait"):
        raise EmailWaitError("none", ReaderCaseKeyword.EMAIL_NOT_RECEIVED, details={"found": 0})
"""})
        reports = pytester.path / "reports"
        result = pytester.runpytest("-p", "emi_harness.plugin", "-p", "no:cacheprovider",
                                    "--emi-reports-dir", str(reports))
        result.assert_outcomes(failed=1)
        result.stdout.fnmatch_lines(["*EMI evidence: plan*", "*EMI evidence: error_details*"])
        outcome = load_results(reports)[0]
        assert outcome.attachments == ("attachments/C400061/01_plan.json",
                                       "attachments/C400061/02_error_details.json")
        job = next(reports.rglob("settings.json")).parent
        assert json.loads((job / outcome.attachments[1]).read_text(encoding="utf-8"))["found"] == 0
        assert json.loads((job / "settings.json").read_text(encoding="utf-8"))["reports_dir"]["source"] == "cli"

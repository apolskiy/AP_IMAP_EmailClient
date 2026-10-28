# SPDX-FileCopyrightText: 2026 Aleksandr Polskiy
# SPDX-License-Identifier: Apache-2.0
"""Unit tests: the merged Allure results show every test that should have run, per system (design §9.8)."""

import json
from pathlib import Path
from typing import Any

from emi_harness import Status, TestOutcome
from emi_harness.allure_merge import collect_job, main
from emi_harness.results import ResultsWriter

_NODE = "tests/functional/emi_func_x.py::TestEMIX::EMI_TOOL_FUNC_400001_sends[C400001-to]"
_ENTRY = {"nodeid": _NODE, "case_id": "C400001", "priority": "P1"}


def _job(directory: Path, job: str, selected: list[dict[str, str]], results: list[dict[str, Any]]) -> Path:
    """A job directory as a run leaves it: collection, results lines and Allure results."""
    job_dir = directory / "reports" / "20261028t120000-abcd" / job
    (job_dir / "allure-results").mkdir(parents=True)
    (job_dir / "collection.json").write_text(json.dumps({"selected": selected}), encoding="utf-8")
    writer = ResultsWriter(job_dir, "main")
    for index, item in enumerate(results):
        writer.write(TestOutcome(nodeid=item["nodeid"], case_id=item["case_id"], priority=item["priority"],
                                 status=Status.PASSED, job=job))
        allure = {"name": "EMI_TOOL_FUNC_400001_sends[C400001-to]", "status": "passed", "historyId": "abc123",
                  "fullName": "tests.functional.emi_func_x.TestEMIX#EMI_TOOL_FUNC_400001_sends",
                  "parameters": [{"name": "field", "value": "to"}], "uuid": f"{directory.name}{index}{job}",
                  "labels": [{"name": "parentSuite", "value": item["priority"]}]}
        # allure-pytest names each result by a random UUID, so two systems never share a file name.
        target = job_dir / "allure-results" / f"{directory.name}{index}{job}-result.json"
        target.write_text(json.dumps(allure), encoding="utf-8")
    (job_dir / "allure-results" / f"att{job}-attachment.json").write_text("{}", encoding="utf-8")
    return job_dir


def _results(output: Path) -> list[dict[str, Any]]:
    """Every result in the merged folder."""
    return [json.loads(path.read_text(encoding="utf-8")) for path in sorted(output.glob("*-result.json"))]


def _no_collector(job: str) -> list[dict[str, str]]:
    """A collector that must not be called."""
    raise AssertionError(f"job {job} was collected although it ran")


class TestEMIHarnessAllureMerge:
    """Each system's results stay apart, and tests that never ran show as broken."""

    def EMI_HARNESS_UNIT_200167_systems_kept_apart(self, tmp_path: Path) -> None:
        """The same test on two systems becomes two results with an OS parameter and different history IDs."""
        _job(tmp_path / "ubuntu", "P1", [_ENTRY], [_ENTRY])
        _job(tmp_path / "windows", "P1", [_ENTRY], [_ENTRY])
        output = tmp_path / "merged"
        code = main([str(output), "--os", f"ubuntu={tmp_path / 'ubuntu'}", "--os",
                     f"windows={tmp_path / 'windows'}", "--expect", "P1"], collector=_no_collector)
        results = _results(output)
        assert code == 0 and len(results) == 2
        assert sorted(result["parameters"][-1]["value"] for result in results) == ["ubuntu", "windows"]
        assert all(result["parameters"][0] == {"name": "field", "value": "to"} for result in results)
        assert len({result["historyId"] for result in results}) == 2
        assert (output / "attP1-attachment.json").is_file() and (output / "categories.json").is_file()

    def EMI_HARNESS_UNIT_200168_job_that_never_ran(self, tmp_path: Path) -> None:
        """A chosen job without a collection: each of its tests is broken with JOB_RESULT_MISSING on each system,
        and its tests are listed once."""
        _job(tmp_path / "ubuntu", "P0", [], [])
        _job(tmp_path / "windows", "P0", [], [])
        calls = []

        def collector(job: str) -> list[dict[str, str]]:
            """P1's one test."""
            calls.append(job)
            return [_ENTRY]

        output = tmp_path / "merged"
        code = main([str(output), "--os", f"ubuntu={tmp_path / 'ubuntu'}", "--os",
                     f"windows={tmp_path / 'windows'}", "--expect", "P0", "--expect", "p1"], collector=collector)
        results = _results(output)
        assert code == 0 and calls == ["P1"] and len(results) == 2
        for result in results:
            assert result["status"] == "broken"
            assert result["statusDetails"]["message"].startswith("[JOB_RESULT_MISSING] job P1 (")
            labels = {label["name"]: label["value"] for label in result["labels"]}
            assert labels["parentSuite"] == labels["tag"] == "P1" and labels["severity"] == "critical"
            assert labels["subSuite"] == "TestEMIX" and labels["suite"] == "emi_func_x"
            assert result["fullName"] == "tests.functional.emi_func_x.TestEMIX#EMI_TOOL_FUNC_400001_sends"
        assert len({result["historyId"] for result in results}) == 2

    def EMI_HARNESS_UNIT_200169_selected_without_result(self, tmp_path: Path) -> None:
        """A test the job selected but wrote no result for is broken with JOB_RESULT_MISSING; no Allure
        results at all exit 2."""
        _job(tmp_path / "ubuntu", "P1", [_ENTRY], [])
        output = tmp_path / "merged"
        code = main([str(output), "--os", f"ubuntu={tmp_path / 'ubuntu'}", "--expect", "P1"],
                    collector=_no_collector)
        results = _results(output)
        assert code == 0 and len(results) == 1
        assert results[0]["statusDetails"]["message"] == ("[JOB_RESULT_MISSING] selected by job P1 (ubuntu) but "
                                                          "no result was written")
        empty = tmp_path / "empty"
        empty.mkdir()
        assert main([str(tmp_path / "none"), "--os", f"ubuntu={empty}"], collector=_no_collector) == 2

    def EMI_HARNESS_UNIT_200170_collects_a_job(self) -> None:
        """``collect_job`` lists a job's tests through pytest without a mail server."""
        selected = collect_job("P0", ["tests"])
        assert selected and all(entry["priority"] == "P0" for entry in selected)
        assert all(entry["case_id"] and "::" in entry["nodeid"] for entry in selected)

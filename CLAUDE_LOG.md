<!--
SPDX-FileCopyrightText: 2026 Aleksandr Polskiy
SPDX-License-Identifier: Apache-2.0
-->
# CLAUDE_LOG: Architectural Decisions and Pylint Scores

Tracks phase milestones, architectural decisions and pylint scores (skill-rules). Detailed decisions
live in `docs/design/phase0_email_client_ambiguities.md`; this log records when they were made.

## Phase log

| Date | Phase | Entry |
|---|---|---|
| 2026-10-26 | 0 | Ambiguity register AMB-01 to AMB-62 resolved with the author; spikes against local Greenmail 2.1.15 recorded as evidence. |
| 2026-10-26 | 0 | **Phase 0 approved** by the author. |
| 2026-10-26 | 2 | Design document rewritten from the approved register; keyword catalog `docs/keywords.md` added; README decisions, left-out and AI-tools sections added. |
| 2026-10-26 | 2 | **Phase 2 approved** by the author (including the five new keywords and the run-ID reading: timestamp + 4 hex per run, worker part per worker). Phase 3 starts with project setup files, then `emi_reader`. |
| 2026-10-26 | 3 | Project setup files (`pyproject.toml`, `.pylintrc`, pinned requirements, `.env.example`). `emi_reader` implemented with 62 unit tests (simulated IMAP server, fault classification, retries, folders); live check against Greenmail passed. Found and recorded the SMTP dot-stuffing pitfall for the sender. |
| 2026-10-26 | 3 | `emi_sender` implemented (templates from the sample `.eml` files, reissued HS256 tokens, To/Cc/Bcc, four distributions, SMTP and IMAP APPEND delivery with retries, manifest, CLI exit codes 0/1/2/3) with 63 unit tests; live check against Greenmail: activation and invitation sent by the CLI were read back by `emi_reader` with a valid token for the recipient, and a wrong port exited with code 3. |
| 2026-10-26 | 3 | Test doubles renamed from `Fake*` to `Sim*` (author's rule); the shared `SimClock` moved to `unittests/sims.py`. `emi_greenmail` implemented (readiness, port probes, readiness wait, configuration, users, purge; no restart) with 19 unit tests; live check against Greenmail passed. New keyword `GREENMAIL_API_REQUEST_FAILED`. |
| 2026-10-26 | 3 | `GREENMAIL_API_REQUEST_FAILED` approved; new keywords `GREENMAIL_SMTP_CONNECTION_FAILURE` / `GREENMAIL_IMAP_CONNECTION_FAILURE` (author's request). `emi_test_broker` implemented (HTTP service, token, leases, connection budget, typed errors over the wire, dated log, auto-start, purge guard) with 51 unit tests incl. real loopback HTTP; live check with real auto-start passed after fixing three start-up bugs it exposed. Pylint test-name pattern widened for multi-word tool names (`EMI_TEST_BROKER_UNIT_<id>`). README: Project Structure section. |
| 2026-10-26 | 3 | Folder names: modified UTF-7 (RFC 3501) implemented; UTF-8 folder names recorded as a limitation (Greenmail has no `UTF8=ACCEPT`; author's decision). `docs/rtm.md` created (design ahead of code; gaps tracked there). README documents the `http.client` choice and every pylint setting and exception. |
| 2026-10-26 | 3 | `emi_api` implemented: run IDs, address builder (`API_ADDRESS_INVALID`), `EmiSettings`, `EmiApi` with `service` (broker client, run lease) and `inprocess` (same `BrokerService` code without HTTP) modes, `health()` that never raises, sender/reader facades, family registry; 46 unit tests. `EmiError` is a tuple of the tools' error bases (an ABC was verified not to work with `except`), documented in README and design §8.2 at the author's request. Live check in both modes against Greenmail passed. |
| 2026-10-26 | 3 | AMB-63 (author's requirement, to make TestRail use easier): the test ID in every sent email is the TestRail case ID `C<number>` (address `qa+C400001_<run id>@…`, `X-EMI-Test-Id`, body marker line). `Marker` converts `400001` to `C400001` and rejects other forms; the sender keeps the local part's case; the reader compares test IDs in TestRail form. A live probe showed Greenmail's `SEARCH TO` is case-insensitive, so the capital `C` is safe. Register, design §4/§8.2/§9, README, keywords and RTM updated. |
| 2026-10-26 | 3 | `emi_harness` started: option table (precedence and sources), step model, outcome classification, job control (fail-fast, critical P0, circuit breaker, job budget, shared stop file), dependency cascade, priorities/downgrades/gates, selection, results files, session setup with probe email, pytest plugin, CLI and gate command; first functional tests (C400001–C400005). The first live run found two broker bugs, both fixed: (1) the auto-started broker rebuilt its settings from `EMI_*` only and failed the `config_hash` check (`TEST_BROKER_VERSION_MISMATCH`); it now gets a settings file. (2) On Windows the broker showed a console window (`DETACHED_PROCESS` plus the venv launcher); it now uses `CREATE_NO_WINDOW`. Rerun: 5 passed. Design: separate repositories for tools, harness and tests noted as the ideal (one repository because of the one-day limit). |
| 2026-10-26 | 3 | `emi_harness` unit tests (75): step model, outcomes, job control, priorities and gates, dependencies, selection, option table (precedence, sources, redaction, CLI/plugin parity), results and calibration, session probe, keyword-catalog completeness, the plugin run inside a throwaway project (`pytester`), and the harness and gate CLIs with pytest replaced. Live: the harness CLI ran 5 functional tests against Greenmail (all passed) and the gate passed. README run instructions, `docs/ci_jobs.md` and RTM updated. Gaps recorded in the RTM: sender/broker CLIs not generated from the option table, settings sources not yet in reports, per-test attachments, 20% time share reported but not enforced. Keywords `CASE_VERIFICATION_FAILED`, `CASE_UNEXPECTED_ERROR`, `HARNESS_DUPLICATE_TEST_ID` approved by the author (2026-10-26) and added to `docs/keywords.md`. |
| 2026-10-26 | 3 | Report evidence (design §9.8): `emi_harness/evidence.py` writes per-test JSON attachments (sender manifests via the new `EmiApi.observe`, a failing tool error's details, `steps.attach`), shows them in the HTML report and Allure, and writes the job's settings with sources to `settings.json`; 6 unit tests. README, at the author's request: a documentation map (which file and design section holds what), Docker and Greenmail container setup and restart (image from Docker Hub `greenmail/standalone`), running from the CLI, and optional Java and Allure installation. |
| 2026-10-26 | 3 | Recipient fields can be chosen per wait (`match_fields=` on every wait, through `EmiApi` and the test broker); before, they were fixed per mailbox, so pooled broker connections could only match `To`. Needed for the Cc/Bcc functional tests (design §13.3). 6 unit tests; live check: Cc via `cc`, Bcc via `delivered-to`, both missed by a `To`-only wait. |

## Key architectural decisions

| Date | Decision | Register |
|---|---|---|
| 2026-10-26 | Packages `emi_reader`, `emi_sender`, `emi_test_broker`, `emi_greenmail`, `emi_api`, `emi_harness`; one folder, unit-test folder and CI job per tool | AMB-56, AMB-62 |
| 2026-10-26 | Stdlib `imaplib` behind a transport interface; read-only waiting by polling | AMB-09, AMB-22 |
| 2026-10-26 | Shared local test broker (port 8025) with auto-start, run leases and dated log | AMB-53, AMB-54 |
| 2026-10-26 | Settings precedence: argument > CLI > env > `.env` > default; one option table | AMB-10, AMB-11 |
| 2026-10-26 | Keyword grammar `<TOOL>_<PROTOCOL?>_<CAUSE>`, `CASE_`, `JOB_` | AMB-61, AMB-62 |
| 2026-10-26 | Functional CI on Windows and Ubuntu; P0 → P1 → P2–P4 chain; gates 100 / 100 / 85 / 95% | AMB-15, AMB-44, AMB-48 |
| 2026-10-26 | Test ID in mail is the TestRail case ID `C<number>`, so emails map to TestRail cases directly | AMB-63 |
| 2026-10-26 | New keywords: `CASE_VERIFICATION_FAILED` (false check or plain `assert`), `CASE_UNEXPECTED_ERROR` (untyped exception, class as secondary), `HARNESS_DUPLICATE_TEST_ID` (collection stops) | `docs/keywords.md` |
| 2026-10-26 | `EmiError` is a tuple of the tools' error bases: tools cannot import `emi_api`, and `except` ignores ABC virtual subclasses | Design §8.2 |
| 2026-10-26 | Sender always serializes with CRLF (`policy.SMTP`) so SMTP dot-stuffing cannot corrupt links; `max-line-length` 120 | Spike Evidence |
| 2026-10-26 | Functional suite of 1,016 cases: every parametrized case is its own TestRail case through a per-parameter `emi(case=...)` marker; a test needing several addresses uses address slots `<run>-rNN` of its own case, so no two tests share an address | Design §4, §13.3 |
| 2026-10-26 | Test validity: named faults as backend wrappers prove each functional test can fail for the right reason; stability repeats find flaky tests and thin timing margins; `CASE_NO_VERIFICATION` for a test that checks nothing; non-blocking `test-validity` job | AMB-64 |
| 2026-10-26 | CI: one reusable functional chain called per OS, so each OS keeps its own P0 → P1 → P2-P4 order; the harness exit code (2 = abort) decides whether the chain goes on; Greenmail started by one composite action on both OSes | Design §12 |
| 2026-10-26 | A P0 failure stays critical but stops nothing: the P0 job finishes, P1 and P2-P4 run, only dependents are skipped; the CI chain shares one run ID and passes earlier jobs' results on, so dependents in later jobs are skipped too | AMB-65 |
| 2026-10-27 | Test validity built and run live: all 9 named faults caught by all 47 catching tests. The runs found two harness bugs (a fault wrapper hid the broker client from `EmiApi`; a keyword enum in a test report crashed xdist workers) and two test gaps (counts and batches compared sets, so an extra email passed; `sender-drops-email` missed batches). Faults never touch the session probe | AMB-64 |
| 2026-10-27 | The gate counts a selected test without a result as not passed (`JOB_RESULT_MISSING`): a CI P2-P4 job hit its 1 h budget after 748 tests, its workers crashed on a keyword enum, the remaining tests left no result, and the gate passed at 758/759 | Design §9.7 |
| 2026-10-27 | Windows CI: Greenmail started through WMI, because a step's child process ends with the step on Windows runners | Design §12 |
| 2026-10-27 | The status workflow is `joint-ci-workflow`; a manual run ticks its functional jobs; P2, P3 and P4 run as three jobs side by side, their 85% measured together by `quality-gate` | AMB-66, AMB-67 |

## Pylint scores

| Date | Scope | Score |
|---|---|---|
| n/a | No Python code yet (design phase) | n/a |
| 2026-10-26 | `emi_reader`, `unittests` (pylint 4.1.3, Python 3.14.2) | **10.00/10** |
| 2026-10-26 | `emi_reader`, `emi_sender`, `unittests` (125 unit tests pass) | **10.00/10** |
| 2026-10-26 | `emi_reader`, `emi_sender`, `emi_greenmail`, `unittests` (144 unit tests pass, no pylint messages) | **10.00/10** |
| 2026-10-26 | `emi_reader`, `emi_sender`, `emi_greenmail`, `emi_test_broker`, `unittests` (199 unit tests pass, no pylint messages) | **10.00/10** |
| 2026-10-26 | all tools incl. `emi_api`, `unittests` (257 unit tests pass, no pylint messages) | **10.00/10** |
| 2026-10-26 | all tools, `unittests`, after AMB-63 (272 unit tests pass, no pylint messages) | **10.00/10** |
| 2026-10-26 | all tools incl. `emi_harness`, `unittests`, `tests`, `conftest.py` (348 unit tests pass, no pylint messages) | **10.00/10** |
| 2026-10-26 | all tools, `unittests`, `tests` with the 1,016-case functional suite, `conftest.py` (365 unit tests pass, no pylint messages) | **10.00/10** |
| 2026-10-27 | all tools, `unittests`, `tests`, `conftest.py`, with test validity, the P2 / P3 / P4 jobs and missing-result gating (408 unit tests pass, no pylint messages) | **10.00/10** |

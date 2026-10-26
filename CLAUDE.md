<!--
SPDX-FileCopyrightText: 2026 Aleksandr Polskiy
SPDX-License-Identifier: Apache-2.0
-->
# CLAUDE.md - AI Collaborator Instructions for AP_IMAP_EmailClient

## Core Directives & Safeguards
1. **NO COMMITS**: Never execute `git commit`, `git push`, or alter git branches directly. You may stage files (`git add`), but all commits are strictly user-executed.
2. **Design First (Phase 0 & 2)**: Never write implementation code before design documents and ambiguity registers are approved.
3. **Pylint 10.00/10**: All Python files must achieve a strict 10/10 rating against `.pylintrc`.
4. **No Single-Character Variables**: Variable names must be at least 3 characters long and descriptive (`index`, `error`, `item_value`). Single-letter variables (`i`, `e`, `x`) are strictly prohibited in all scopes.
5. **Type Annotations & Docstrings**: Every parameter and return must be explicitly typed (including `-> None`). Google-style docstrings are mandatory.

## Directory Structure
Each tool has its own folder, its own unit-test folder and its own standalone CI job.
* `emi_reader/`: IMAP email client (reader) library, IMAP connection wrappers, parsers, and link extractors.
* `emi_sender/`: Email sender client (JSON templates, SMTP/APPEND delivery, batch distributions).
* `emi_test_broker/`: Test broker: background process (default port 8025) that calls the sender and reader on behalf of all runs on a machine and shares their connections and log.
* `emi_greenmail/`: Greenmail client (readiness, users, purge via Greenmail's REST API).
* `emi_api/`: Central public API (`EmiApi`), the single entry point tests call.
* `emi_harness/`: Test runner orchestration, parallel execution logic, keyword loggers, and dependency graph evaluators.
* `unittests/`: Core unit tests, one folder per tool:
  * `unittests/emi_reader/`: Reader library unit tests (CI job `unit-emi-reader`).
  * `unittests/emi_sender/`: Sender unit tests (CI job `unit-emi-sender`).
  * `unittests/emi_test_broker/`: Test broker unit tests (CI job `unit-emi-test-broker`).
  * `unittests/emi_greenmail/`: Greenmail client unit tests (CI job `unit-emi-greenmail`).
  * `unittests/emi_harness/`: Harness unit tests (CI job `unit-emi-harness`).
  * `unittests/emi_api/`: Central public API (`EmiApi`) unit tests (main pipeline only).
* `tests/`: Functional and non-functional test partitions scaling to 1,000+ test cases.
* `docs/design/`: Design documents and Phase 0 ambiguity registers.
* `reports/`: Generated JUnit XML, HTML, and Allure outputs.

## Test Naming & TestRail Integration
* All test functions and methods carry TestRail-compatible ID prefixes with unique numbers **at least 6 digits long** (e.g., `EMI_TOOL_100001` or `C100001`).
* Test failure reporting must include a **Primary Keyword** and relevant **Secondary Keywords** mapped strictly to the failure cause.
* Keyword grammar: tool failures `<TOOL>_<PROTOCOL?>_<CAUSE>` with prefixes `READER_`, `SENDER_`, `TEST_BROKER_`, `GREENMAIL_`, `API_`, `HARNESS_` (a protocol never stands alone); test-case failures `CASE_<CAUSE>`; job decisions `JOB_<CAUSE>`.

## Environment & Local Services
* Test Email Server: Greenmail standalone running locally from `C:\Users\vorns\greenmail` via `greenmail_start.sh`.
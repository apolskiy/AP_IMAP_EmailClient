<!--
SPDX-FileCopyrightText: 2026 Aleksandr Polskiy
SPDX-License-Identifier: Apache-2.0
-->
# Requirements Traceability Matrix

The design ([docs/design/email_client_harness_design.md](design/email_client_harness_design.md)) describes the
whole intended system, including parts not built yet. This matrix traces each requirement from the design
(and the register rows it implements) to the code, the unit tests and the live evidence, and records
its status. Updated with every implementation step.

**Status:** **Done**: implemented, unit-tested, and checked live against Greenmail where it talks to a
server. **Partial**: implemented in part; the gap is named. **Planned**: designed, not implemented.
Unit test IDs are written without the name, e.g. `100060` for `EMI_READER_UNIT_100060_...`.
Live evidence is in the register's Spike Evidence table.

## Configuration, identifiers, keywords

| ID | Requirement | Design | Code | Unit tests | Status |
|---|---|---|---|---|---|
| R-CFG-01 | Settings objects with validated ranges; default ports per security mode | §3 | `emi_reader/settings.py`, `emi_sender/settings.py`, `emi_greenmail/settings.py`, `emi_test_broker/settings.py` | 100001, 100002, 110033, 120040, 130003 | Done |
| R-CFG-02 | Precedence: argument > CLI > env > `.env` > default | §3 | `emi_harness/options.py` (one option table for the harness CLI and the pytest plugin), `emi_sender/cli.py`, `emi_test_broker/settings.py` (`settings_from_env`) | 110060, 200060–200065 | Partial: the harness CLI and pytest plugin share the table (parity test 200063); the sender and broker CLIs still define their own options instead of being generated from it |
| R-CFG-03 | Secrets only from env / `.env`, redacted everywhere | §3 | every `describe()`, sender manifest, broker `/health`, Greenmail `create_user` log | 100003, 110056, 120022, 130001 | Done |
| R-CFG-04 | `/health` and PRE steps report each setting **with its source** | §3, §7 | `emi_harness/options.py` (`Resolved.describe`), `emi_test_broker/service.py` (settings only) | 130001, 200060, 200062 | Partial: the harness writes every setting with its source to `reports/<run>/<job>/settings.json` (secrets redacted; 200123, 200124); the broker's `/health` shows values without sources |
| R-ID-01 | Run ID `<yyyymmddthhmmss>-w<worker>-<4hex>`, address builder, `API_ADDRESS_INVALID` | §4 | `emi_api/identity.py` | 300001–300005, 300010–300017, 300047 | Done (address slots `<run>-rNN` give one test up to 99 more addresses) |
| R-ID-02 | Marker headers and body line; ASCII-only IDs (Cyrillic rejected) | §4 | `emi_sender/compose.py` | 110005, 110009, 110010, 130011, 130053 | Done |
| R-ID-03 | Test ID in mail is the TestRail case ID `C<number>` (address, header, body line); `400001` accepted and converted; reader matches either form (AMB-63) | §4 | `emi_sender/compose.py` (`testrail_case_id`, `Marker`), `emi_api/identity.py` (`case_id`), `emi_reader/mailbox.py` | 110005, 110009, 110065, 100114, 300010–300016 | Done |
| R-KW-01 | Every failure carries primary + secondary keywords per the catalog | §9.3, `docs/keywords.md` | every tool's `keywords.py` / `errors.py`, `emi_harness/outcomes.py` (`classify`) | throughout, 200015–200017, 200090, 200100 | Partial: every keyword in code is in the catalog (200090); generating `docs/keywords.md` from the code is planned |

## Reader (`emi_reader`)

| ID | Requirement | Design | Code | Unit tests | Status |
|---|---|---|---|---|---|
| R-RD-01 | `imaplib` transport behind an interface; original socket error kept; one lock per connection | §5.1 | `transport.py` | 100040, 100041 | Done |
| R-RD-02 | Folder names in modified UTF-7 (UTF-8 `UTF8=ACCEPT` is a documented limitation) | §5.1, §15 | `folders.py`, `transport.py` | 100110–100113 | Done |
| R-RD-03 | One connection per worker / pool slot, reused; `NOOP` check | §5.2 | broker pool in `emi_test_broker/service.py`, `-n` cap in `emi_harness/cli.py` | 130014, 130015, 200110 | Partial: broker pool and the `-n` cap (workers lowered to `max_connections`) done; a dedicated connection per worker in `inprocess` mode is planned |
| R-RD-04 | Accessibility probe and matrix; `NONE_ACCESSIBLE`; filter evidence | §5.3 | `accessibility.py`, `mailbox.py` | 100050–100053, 100094, 100100, 100101 | Partial: one account only (multi-account scope planned); `expected_folder` skip (`READER_MAILBOX_EXPECTED_FOLDER_INACCESSIBLE`) planned |
| R-RD-05 | Read-only wait: pre-arrived search, new-UID polling, `UIDVALIDITY` re-search | §5.4 | `mailbox.py`, `transport.py` | 100060, 100061, 100065, 100102, 100118, 100119, 100120 | Partial: in service mode each wait polls on its own; one shared poll loop for all waits is planned. Waits poll by search and fetch their candidates in one command (AMB-71; single-email wait 1.84-1.99 s to 1.07-1.12 s at 5,054 messages). Each fetch still costs more as Greenmail's folder fills (about 0.7 s at 5,000 messages) |
| R-RD-06 | Exact recipient match on `match_fields` (default `To`, choosable per wait through `EmiApi` and the broker) + CTA + marker | §5.4 | `mailbox.py`, `mime.py`, `emi_test_broker/service.py`, `client.py`, `emi_api/api.py`, `backends.py` | 100062–100064, 100070–100072, 100115, 100116, 130078, 130079, 300049 | Done (live: Cc via `cc`, Bcc via `delivered-to`, both missed by a `To`-only wait) |
| R-RD-07 | Duplicates, count exceeded with settle window (choosable per wait through `EmiApi` and the broker), deadline keyword rules | §5.4 | `mailbox.py`, `emi_test_broker/service.py`, `client.py`, `emi_api/api.py`, `backends.py` | 100080–100083, 100092, 100117, 300062 | Done |
| R-RD-08 | Retry only healable faults, capped backoff, attempt history; a refused reconnect after a working connection is retried, so a wait rides through a server restart (AMB-78) | §5.5 | `transport.py`, `mailbox.py` | 100040, 100090–100093, 100134, 100135, 100133 | Done |
| R-RD-09 | MIME parsing; CTA by exact label; text fallback; mismatch fail/warn; ambiguous; strategy chain; CTA registry | §5.6 | `mime.py`, `links.py` | 100010–100012, 100020–100028 | Done |
| R-RD-10 | Token helpers `token_subject`, `assert_link_for` | §5.7 | `tokens.py` | 100030–100032 | Done |
| R-RD-11 | Mailbox inventory: totals and per-category before/after, JSON records, taken from each wait (AMB-76) | §5.8 | `emi_reader/inventory.py`, `emi_reader/mailbox.py`; `emi_test_broker/service.py`, `client.py`; `emi_api/api.py`, `backends.py`; `emi_harness/inventory_log.py`, `plugin.py` | 100123–100125, 130081, 300051, 300063, 200164 | Done (live: C400001, C400006, the batch C401009 and the session probe each logged under their own case, no extra fetch) |

## Sender (`emi_sender`)

| ID | Requirement | Design | Code | Unit tests | Status |
|---|---|---|---|---|---|
| R-SD-01 | Templates on the samples; encodings preserved; generic without links | §6.1 | `compose.py`, `templates/` | 110002, 110003, 110007 | Done |
| R-SD-02 | HS256 tokens: `sub`, `exp` = Date + 24 h, `site` kept, `jti` | §6.2 | `tokens.py` | 110001, 110004, 110008 | Done |
| R-SD-03 | Distributions `all_to_one`, `even_5`, `uneven_5`, `one_each_10`; seeded order; explicit plan | §6.3 | `batch.py` | 110020–110025 | Done |
| R-SD-04 | Recipient field `To` / `Cc` / `Bcc` (+ `Delivered-To`) **or combinations** | §6.3 | `compose.py` | 110006 | Partial: one field per email; combinations planned |
| R-SD-05 | SMTP routing to the base mailbox; `APPEND`; CRLF serialization | §6.4 | `delivery.py` | 110011, 110040, 110041 | Done |
| R-SD-06 | Sender CLI goes through the broker by default; `--direct` bypasses | §6.4 | `cli.py` (direct only) | n/a | Partial: CLI always sends directly; broker path planned |
| R-SD-07 | Validation: negative → 0 + logged; non-int / out of range → exit 2 | §6.5 | `batch.py`, `cli.py` | 110030–110034, 110061–110063 | Done |
| R-SD-08 | Manifest; exit codes 0 / 1 / 2 / 3; send and batch budgets | §6.6, §10 | `sender.py` | 110050–110056, 110060, 110064 | Done |

## Test broker (`emi_test_broker`)

| ID | Requirement | Design | Code | Unit tests | Status |
|---|---|---|---|---|---|
| R-TB-01 | Loopback HTTP service, token, typed errors over the wire | §7 | `server.py`, `client.py`, `codec.py` | 130040–130043, 130050–130054 | Done |
| R-TB-02 | `/send`, `/wait` (link and emails), `/emails` (a failed test's raw emails, AMB-74), connection budget with queueing | §7, §11 | `service.py`, `leases.py`, `codec.py`; `emi_reader/mailbox.py` (`emails_to`) | 130010–130017, 130023, 130075, 130080, 100121, 100122 | Done |
| R-TB-03 | `/inventory` | §7 | answers 501 | n/a | Not built: the inventory comes with each `/wait` answer (AMB-76, R-RD-11) |
| R-TB-04 | Lifecycle `start / stop / status / serve`; auto-start once; idle shutdown | §7 | `cli.py`, `client.py`, `server.py` | 130057, 130060–130064, 130073, 130076 | Done |
| R-TB-05 | Port rules: reuse, `PORT_UNAVAILABLE`, `VERSION_MISMATCH`; exclusive bind | §7 | `server.py`, `client.py` | 130055, 130062, 130063 | Done |
| R-TB-06 | Dated log, ISO 8601 with offset, `_n` fallback, debug-report entries | §7 | `broker_log.py` | 130070–130072, 200162 | Done: a failed or skipped test's report carries the lines of its window that name it (AMB-74) |
| R-TB-07 | Run leases with heartbeat and expiry; waits count as active | §7 | `leases.py`, `client.py` (`LeaseKeeper`) | 130020–130022, 130056 | Done |
| R-TB-08 | Purge refused while runs are active; marker-only default; dry run; `--older-than`, `--all` | §7 | `purge.py`, `service.py`, `cli.py` | 130030–130036, 130074 | Done |

## Greenmail client (`emi_greenmail`)

| ID | Requirement | Design | Code | Unit tests | Status |
|---|---|---|---|---|---|
| R-GM-01 | Readiness + port probes; never starts Greenmail; readiness wait | §8.1 | `client.py` | 120001–120007 | Done |
| R-GM-02 | Readiness keywords: `NOT_READY`, `SMTP_` / `IMAP_CONNECTION_FAILURE` | §8.1 | `client.py` | 120003, 120008–120011 | Done |
| R-GM-03 | Configuration, users (list / create / delete), purge; `API_REQUEST_FAILED` | §8.1 | `client.py` | 120020–120027 | Done |

## Central API, harness, CI, functional tests

| ID | Requirement | Design | Code | Unit tests | Status |
|---|---|---|---|---|---|
| R-API-01 | `EmiApi`: settings, mode (`service` / `inprocess`), `start_run`, `health`, `sender`, `reader`, `greenmail`; plugin registry | §8.2 | `emi_api/api.py`, `emi_api/backends.py`, `emi_api/settings.py` | 300020–300023, 300040–300053, 300060, 300061, 300063 | Partial: the inventory reaches the observers after each wait (AMB-76) instead of a `reader.inventory` call; sources of settings are known to the harness (R-CFG-04), not to `EmiSettings` |
| R-API-02 | `EmiError` catches every tool's typed error with its keywords, identical in both modes | §8.2 | `emi_api/errors.py` | 300030, 300031, 300043, 300052 | Done |
| R-HN-01 | Harness CLI: test, file, keyword, priority, Allure, calibrate; exit codes; upstream-abort cascade | §9.1 | `emi_harness/cli.py`, `selection.py`, `plugin.py`, `results.py` (`calibrate`) | 200050–200054, 200072, 200104, 200107, 200108, 200110–200113 | Done (live: functional tests pass through the CLI; a parametrized case is selected by its own `C<number>`) |
| R-HN-02 | Step model: PRE, numbered steps, verify, cleanup, step/test time limits | §9.2, §10 | `emi_harness/steps.py`, `outcomes.py`, `plugin.py` | 200001–200010, 200103 | Done (time limits are checked when a step or test returns, not by interrupting it) |
| R-HN-03 | Failing fast, circuit breaker, dependency skipping; a P0 failure stops nothing, dependents are skipped across jobs (AMB-65); a parent of a lower priority is refused (`HARNESS_DEPENDENCY_ORDER`) | §9.4, §9.5 | `emi_harness/jobs.py`, `dependencies.py`, `plugin.py`, `cli.py`; `.github/actions/harness-job` (earlier jobs' results in CI) | 200020–200025, 200040–200044, 200101, 200102, 200109, 200112, 200115 | Done (CI part: not yet run on GitHub) |
| R-HN-04 | Session setup and probe email | §9.6 | `emi_harness/session.py` | 200080–200083 | Done (live) |
| R-HN-05 | Priorities, downgrades, gates; a selected test without a result counts as not passed, and a chosen job without a `collection.json` fails the gate (`JOB_RESULT_MISSING`) | §9.7 | `emi_harness/priorities.py`, `gate.py` (`--expect`), `plugin.py` (`collection.json` lists the selection), `ci-regression-run.yml` (one directory per OS) | 200030–200037, 200104, 200114, 200125, 200147, 200148, 200163 | Done: the 20% test-count ceiling is enforced; P0 + P1 above 30% of test time is a `JOB_PRIORITY_TIME_SHARE` warning that never fails the gate (AMB-75) |
| R-HN-06 | Reports: JUnit, HTML, optional Allure (priorities as labels, failure keywords as categories, AMB-69), job counts in the log and the GitHub step summary (AMB-72), per-test attachments, a failed or skipped test's raw emails and broker log lines (AMB-74) | §9.8 | `emi_harness/cli.py`, `plugin.py` (JUnit properties, results files), `evidence.py`, `failure_evidence.py`, `allure_report.py`, `summary.py`, `gate.py`; `emi_api/api.py` (`observe`, `reader.emails_to`) | 200071, 200100, 200110, 200120–200124, 200149–200162, 300048 | Partial: JUnit XML with keyword properties, HTML, Allure results with priority labels and keyword categories, job counts and the gate report on the run page (live: P0 5 of 5, gate PASS), results files, the job's settings with sources, per-test evidence (sender manifests, a tool error's attempt history and folder accessibility matrix, `steps.attach`), and for a failed or skipped test every email at its addresses as `.eml` plus its broker log lines (live: C400001 and the 10-email batch C401009 under a named fault) as files, HTML sections and Allure attachments done; each wait's mailbox inventory attached and logged (R-RD-11) |
| R-CI-01 | Regression run `ci-regression-run` (manual run with chosen functional jobs, AMB-66; P2, P3, P4 as jobs side by side, AMB-67), standalone unit jobs, debug job, overlapping-run rules | §12 | `.github/workflows/` (`ci-regression-run.yml`, `functional-chain.yml`, `unit-standalone.yml`, `unit-tool.yml`, `ci-debug-run.yml`), `.github/actions/` (`harness-job`, `start-greenmail`); `emi_harness/priorities.py`, `cli.py` (jobs P2, P3, P4) | 200027, 200112, 200113 (actionlint clean) | Partial: every job runs on GitHub on both operating systems (runs 40437111627 and 40437565154 passed all 1,016 tests then in the suite; Windows needed Greenmail started through WMI); documentation never triggers a run; the debug job's `docker` marker waits for layer-3 fault tests (R-TS-02); the queue rule for a shared real mailbox applies when a Gmail job is added |
| R-TS-01 | Network faults layer 1 (simulated transport), marked `network` (AMB-73; 50 collected tests, counted per tool in the README) | §13.2 | `unittests/` (all tools), `pyproject.toml` (marker) | 100040, 100041, 100051, 100090–100093, 110040, 110041, 110051, 110052, 110064, 120002, 120003, 120008–120010, 120026, 130017, 130035, 130055, 130060, 130061, 130063, 130064, 130073, 300045, 300053 | Done |
| R-TS-02 | Network faults layer 2 (fault proxy) and layer 3 (real outage, `docker` marker) | §13.2 | `unittests/netfaults/` (proxy, simulated IMAP and SMTP servers), `unittests/outage/`; `.github/workflows/unit-outage.yml` | 100126–100131, 110066–110068 (layer 2); 100132, 100133, 110069, 300064 (layer 3) | Done: layer 2 passes on Windows and on Linux (a Python 3.14 container; CI run 40450901295 had found the proxy's `refuse` mode leaking on Linux, fixed); layer 3 passed in `unit-outage` run 40450901475 and against Docker Desktop, including a wait that rides through a restart (AMB-78) |
| R-TS-03 | Functional tests, 1,000+ cases, unique IDs 400000–599999 | §13.3 | `tests/functional/` (`catalog.py` holds the ID blocks; `emi_func_delivery`, `_counts`, `_fields`, `_markers`, `_timing`, `_isolation`, `_batches`) | 400001–400008, 401000–401089, 401500–401508, 402000–402008, 403000–403014, 404000–404014, 406000–406008, 410000–410863 | Partial: 1,019 cases, each its own TestRail case (P0 5: one email per kind, activation and invitation links and a generic email with no link, and per link kind a wait that skips two non-matching emails for a later matching one, AMB-68; P1 9; so P0 + P1 is 1%; P2 406, P3 311, P4 288, batches split by seed so the three parallel jobs send about the same mail, AMB-70); live: every non-batch case (152) and a 16-case batch sample (4 per preset) pass. Not covered yet: recipients in `Delivered-To` / `X-Original-To` alone and field combinations (needs R-SD-04), the transport as a parameter (it is chosen per run, so `APPEND` runs the suite as a whole), parallel runs and unmarked manual mail, accessibility (missing folder, second account, unreachable host), and link variants (text fallback, plain-text-only, HTML/text mismatch, `sub` mismatch), which the current templates cannot produce |
| R-TV-01 | Named-fault catalog as backend wrappers; `--emi-fault`; `validity faults` reports every catching test that did not fail with an expected keyword | §13.4 | `emi_harness/faults.py`, `validity.py`, `options.py`, `plugin.py`; `emi_api/api.py` (wrapped broker client); `tests/functional/validity.py` (catch table) | 200129–200146, 300054 | Done: live against Greenmail, all 9 faults caught by all 47 catching tests |
| R-TV-02 | Stability: N repeats on a healthy setup, flaky / failing tests, thin timing margins | §13.4 | `emi_harness/validity.py`; `.github/workflows/test-validity.yml` | 200141, 200145 | Done: live sample against Greenmail, P1 repeated 3 times, no findings; weekly CI run not yet seen on GitHub |
| R-TV-03 | `CASE_NO_VERIFICATION` for a test that checks nothing; an unsatisfied `pytest.raises` is `CASE_VERIFICATION_FAILED` | §13.4 | `emi_harness/plugin.py`, `outcomes.py`, `steps.py`; `pyproject.toml` (`enable_assertion_pass_hook`) | 200018, 200116–200118 | Done |
| R-TV-04 | Gate refuses results produced under a fault; `test-validity.yml` (manual and weekly, non-blocking) | §12, §13.4 | `emi_harness/gate.py`; `.github/workflows/test-validity.yml` | 200119 (actionlint clean) | Partial: the gate refuses fault results; `test-validity.yml` written and linted, not yet run on GitHub |
| R-TS-04 | Duplicate test-ID check across the suite | §4, §13.1 | `emi_harness/selection.py` (`duplicate_ids`), `plugin.py` | 200054, 200105 | Done (`HARNESS_DUPLICATE_TEST_ID`) |

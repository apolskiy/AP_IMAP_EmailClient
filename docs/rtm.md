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
| R-ID-01 | Run ID `<yyyymmddthhmmss>-w<worker>-<4hex>`, address builder, `API_ADDRESS_INVALID` | §4 | `emi_api/identity.py` | 300001–300005, 300010–300016, 300047 | Done |
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
| R-RD-05 | Read-only wait: pre-arrived search, new-UID polling, `UIDVALIDITY` re-search | §5.4 | `mailbox.py` | 100060, 100061, 100065, 100102 | Partial: in service mode each wait polls on its own; one shared poll loop for all waits is planned |
| R-RD-06 | Exact recipient match on `match_fields` (default `To`) + CTA + marker | §5.4 | `mailbox.py`, `mime.py` | 100062–100064, 100070–100072 | Done |
| R-RD-07 | Duplicates, count exceeded with settle window, deadline keyword rules | §5.4 | `mailbox.py` | 100080–100083, 100092 | Done |
| R-RD-08 | Retry only healable faults, capped backoff, attempt history | §5.5 | `transport.py`, `mailbox.py` | 100040, 100090–100093 | Done |
| R-RD-09 | MIME parsing; CTA by exact label; text fallback; mismatch fail/warn; ambiguous; strategy chain; CTA registry | §5.6 | `mime.py`, `links.py` | 100010–100012, 100020–100028 | Done |
| R-RD-10 | Token helpers `token_subject`, `assert_link_for` | §5.7 | `tokens.py` | 100030–100032 | Done |
| R-RD-11 | Mailbox inventory: totals and per-category before/after, JSON records | §5.8 | n/a | n/a | Planned |

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
| R-TB-02 | `/send`, `/wait` (link and emails), connection budget with queueing | §7, §11 | `service.py`, `leases.py` | 130010–130017, 130023, 130075 | Done |
| R-TB-03 | `/inventory` | §7 | answers 501 | n/a | Planned (needs R-RD-11) |
| R-TB-04 | Lifecycle `start / stop / status / serve`; auto-start once; idle shutdown | §7 | `cli.py`, `client.py`, `server.py` | 130057, 130060–130064, 130073, 130076 | Done |
| R-TB-05 | Port rules: reuse, `PORT_UNAVAILABLE`, `VERSION_MISMATCH`; exclusive bind | §7 | `server.py`, `client.py` | 130055, 130062, 130063 | Done |
| R-TB-06 | Dated log, ISO 8601 with offset, `_n` fallback, debug-report entries | §7 | `broker_log.py` | 130070–130072 | Partial: entries collected; attaching them to reports is part of R-HN-06 (planned) |
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
| R-API-01 | `EmiApi`: settings, mode (`service` / `inprocess`), `start_run`, `health`, `sender`, `reader`, `greenmail`; plugin registry | §8.2 | `emi_api/api.py`, `emi_api/backends.py`, `emi_api/settings.py` | 300020–300023, 300040–300053, 300060, 300061 | Partial: `reader.inventory` waits for R-RD-11; sources of settings are known to the harness (R-CFG-04), not to `EmiSettings` |
| R-API-02 | `EmiError` catches every tool's typed error with its keywords, identical in both modes | §8.2 | `emi_api/errors.py` | 300030, 300031, 300043, 300052 | Done |
| R-HN-01 | Harness CLI: test, file, keyword, priority, Allure, calibrate; exit codes; upstream-abort cascade | §9.1 | `emi_harness/cli.py`, `selection.py`, `plugin.py`, `results.py` (`calibrate`) | 200050–200054, 200072, 200104, 200110–200113 | Done (live: 5 functional tests pass through the CLI) |
| R-HN-02 | Step model: PRE, numbered steps, verify, cleanup, step/test time limits | §9.2, §10 | `emi_harness/steps.py`, `outcomes.py`, `plugin.py` | 200001–200010, 200103 | Done (time limits are checked when a step or test returns, not by interrupting it) |
| R-HN-03 | Failing fast, circuit breaker, dependency skipping | §9.4, §9.5 | `emi_harness/jobs.py`, `dependencies.py`, `plugin.py` | 200020–200025, 200040–200043, 200101, 200102 | Done |
| R-HN-04 | Session setup and probe email | §9.6 | `emi_harness/session.py` | 200080–200083 | Done (live) |
| R-HN-05 | Priorities, downgrades, gates | §9.7 | `emi_harness/priorities.py`, `gate.py` | 200030–200037, 200104, 200114 | Partial: the 20% test-count rule is enforced; the 20% time share is reported by the gate but not enforced |
| R-HN-06 | Reports: JUnit, HTML, optional Allure, per-test attachments | §9.8 | `emi_harness/cli.py`, `plugin.py` (JUnit properties, results files), `evidence.py`; `emi_api/api.py` (`observe`) | 200071, 200100, 200110, 200120–200124, 300048 | Partial: JUnit XML with keyword properties, HTML, Allure results, results files, the job's settings with sources, and per-test evidence (sender manifests, a tool error's attempt history and folder accessibility matrix, `steps.attach`) as files, HTML sections and Allure attachments done; the raw email, broker log entries and mailbox inventory (R-RD-11) are planned |
| R-CI-01 | Main pipeline, standalone unit jobs, debug job, overlapping-run rules | §12 | n/a | n/a | Planned |
| R-TS-01 | Network faults layer 1 (simulated transport) | §13.2 | `unittests/` (all tools) | 100040, 100090–100094, 110040, 110041, 110051, 110052, 120002 | Done |
| R-TS-02 | Network faults layer 2 (fault proxy) and layer 3 (real outage, `docker` marker) | §13.2 | n/a | n/a | Planned |
| R-TS-03 | Functional tests, 1,000+ cases, unique IDs 400000–599999 | §13.3 | `tests/functional/emi_func_delivery.py` | 400001–400005 | Partial: 5 functional tests (live: pass); the 1,000+ suite is planned |
| R-TS-04 | Duplicate test-ID check across the suite | §4, §13.1 | `emi_harness/selection.py` (`duplicate_ids`), `plugin.py` | 200054, 200105 | Done (`HARNESS_DUPLICATE_TEST_ID`) |

<!--
SPDX-FileCopyrightText: 2026 Aleksandr Polskiy
SPDX-License-Identifier: Apache-2.0
-->
# Phase 2: Design Document - AP_IMAP_EmailClient

> **Status:** Phase 2 design, built from the approved Phase 0 register
> (`docs/design/phase0_email_client_ambiguities.md`, AMB-01 to AMB-62, approved 2026-10-26).
> Each section names the register rows it implements; when this document and a register row
> disagree, the register's latest resolution wins and this document is corrected.
> Companion documents: `docs/keywords.md` (keyword catalog), `docs/ci_jobs.md` (CI jobs, user-facing).

## 1. Purpose and Scope

End-to-end tests of an application must wait for emails the application sends asynchronously
(account activation, site invitation, password reset, …) and extract the link behind the email's main
call-to-action (CTA) button. All emails go to **one shared mailbox** that keeps earlier runs, and many
tests run **in parallel**, possibly on several machines at once.

The project delivers:
* a **server-agnostic IMAP reader library** that waits for a test's email and extracts its CTA link;
* a **sender tool** that plays the application and delivers realistic emails built from the samples;
* a **test harness** (runner, gates, keywords, reports) with **functional, unit and network-fault tests**;
* setup for **Greenmail** (the target server, locally and in CI). Any other IMAP/SMTP server works by
  configuration (AMB-29).

Language: **Python 3.14** (`requires-python >=3.14`, AMB-32).

## 2. Architecture (AMB-30, AMB-33, AMB-42, AMB-53, AMB-56, AMB-62)

One `emi_` family: the same word names the package, its unit-test folder, its standalone CI job and its
keyword prefix.

| Package | Role | Unit tests / CI job | Keyword prefix |
|---|---|---|---|
| `emi_reader/` | IMAP reader: connection, search, wait, MIME parsing, CTA link extraction, token helpers | `unittests/emi_reader/`, `unit-emi-reader` | `READER_` |
| `emi_sender/` | Sender: JSON templates on the sample `.eml` files, SMTP / IMAP `APPEND` delivery, batch distributions, manifest | `unittests/emi_sender/`, `unit-emi-sender` | `SENDER_` |
| `emi_test_broker/` | Local background service (port 8025) that runs the sender and reader for every run on the machine, sharing connections, run leases and one log | `unittests/emi_test_broker/`, `unit-emi-test-broker` | `TEST_BROKER_` |
| `emi_greenmail/` | Greenmail client: readiness, configuration and users via Greenmail's REST API (8080) | `unittests/emi_greenmail/`, `unit-emi-greenmail` | `GREENMAIL_` |
| `emi_api/` | Central public API `EmiApi`: the only entry point tests use | `unittests/emi_api/` (main pipeline only) | `API_` |
| `emi_harness/` | Runner: CLI, option table, step model, priorities, gates, dependency skipping, circuit breaker, keywords, reports | `unittests/emi_harness/`, `unit-emi-harness` | `HARNESS_` |
| `tests/` | Thin functional tests calling `EmiApi` only | n/a | `CASE_` (test-case outcomes) |

**Dependency direction** (no cycles): `tests → emi_api → {emi_test_broker client | emi_reader, emi_sender} + emi_greenmail`;
`emi_harness` wraps pytest and reads keywords from every package; `emi_reader` and `emi_sender` never import each other.

**Client modes** (AMB-53): `--emi-client-mode=service` (default) sends every send/wait request to
`emi_test_broker`; `inprocess` calls `emi_reader` / `emi_sender` directly. The broker is a transport
layer over the same library code, never a second implementation.

**Repository layout (trade-off, user decision 2026-10-26)**: ideally the **tools** (`emi_reader`,
`emi_sender`, `emi_test_broker`, `emi_greenmail`, `emi_api`), the **harness** (`emi_harness`) and the
**test cases** (`tests/`) would live in three separate repositories with versioned releases. A change to
one of them would then be validated against the last green release of the other two before it is
accepted, so a failure points at the one repository that changed. This project keeps all three in one
repository because only one working day was available. The package boundaries, the one-way dependency
direction and the standalone per-tool unit jobs keep a later split mechanical.

**Extensibility** (AMB-47): settings accept a list of server endpoints and workers could be pinned to
one each, but this project implements **one sender and one reader against one server** (1 : 1).

## 3. Configuration (AMB-10, AMB-11, AMB-22, AMB-23, AMB-29)

* **Settings objects**: `ImapSettings(host, port, security, username, password, mailbox, connect_timeout,
  read_timeout, verify_tls, ca_file)` and the matching `SmtpSettings`. `security` is `plain`, `ssl` or
  `starttls`; each mode has its own default port (Greenmail: IMAP 3143 / IMAPS 3993, SMTP 3025), every
  one overridable. Default host `127.0.0.1` (on Windows `localhost` may resolve to `::1` first).
* **Precedence**, highest first: **argument in code > CLI option > real environment variable > `.env`
  file > built-in default**. A real environment variable beats `.env`, so a CI secret or one-off
  override never requires editing `.env`. There is no other config file.
* **One option table** (`emi_harness/options.py`) defines each option once: name, `EMI_*` env var,
  type, default, allowed range, help text, secret flag. Both the pytest options (`pytest_addoption` in
  `conftest.py`) and every tool's `argparse` parser are generated from it; a unit test checks that both
  forms match. `emi_harness.cli` passes its options through to pytest unchanged.
* **Default credentials** (user decision 2026-10-26): the built-in defaults are the local Greenmail
  test user `qa` / `password`, public in the README's Docker command, so a fresh clone runs against
  local Greenmail with no setup. Teams that want their own password start Greenmail with
  `-Dgreenmail.users=qa:<password>@example.com` and set `EMI_IMAP_PASSWORD` / `EMI_SMTP_PASSWORD` in
  `.env` (CLI) or as repository secrets (CI); the CI workflow passes the same secret to the Greenmail
  start step and to the tests. Every password other than the public default is treated as a secret.
* **Secrets**: options flagged secret (passwords) have **no CLI flag**; they come only from environment
  variables (CI job secrets) or `.env` (CLI, git-ignored, read with `python-dotenv`). `.env.example`
  lists names only. Secrets are redacted (`***`) in `repr`, logs, reports, exceptions, `/health`
  output and IMAP/SMTP transcripts; a unit test asserts a known password appears nowhere.
* **Parameter names** (README): `EMI_IMAP_HOST`, `EMI_IMAP_PORT`, `EMI_IMAP_SECURITY`, `EMI_IMAP_USER`,
  `EMI_IMAP_PASSWORD`, `EMI_SMTP_HOST`, `EMI_SMTP_PORT`, `EMI_SMTP_SECURITY`, `EMI_SMTP_USER`,
  `EMI_SMTP_PASSWORD`, `EMI_MAX_CONNECTIONS`, `EMI_TEST_BROKER_PORT`, `EMI_CLIENT_MODE`, plus one per
  timeout in §10.
* **Visibility**: `/health` and every test's PRE step report each setting in effect with its source
  (argument / CLI / env / `.env` / default), so a failure can be judged against the values used.

## 4. Identifiers, Addresses and Markers (AMB-18, AMB-20, AMB-41, AMB-50, AMB-63)

* **Test callable**: `EMI_<MODULE>_<LAYER>_<ID>_<behavior>`, module `emi_<layer>_<component>.py`,
  class `TestEMI<Component>`. pytest is configured with `python_files = emi_*.py`,
  `python_classes = TestEMI*`, `python_functions = EMI_*`; pylint's `function-rgx` is relaxed for
  test folders only.
* **TestRail ID**: digits, at least 6 (`\d{6,}`), unique; a unit test rejects duplicates. Test names carry
  the number (`EMI_TOOL_400001_…`). **In mail the test ID is the TestRail case ID `C<number>`**
  (`C400001`), so each email can be matched to its test case in TestRail without conversion (AMB-63,
  user decision to make TestRail use easier). `400001`, `C400001` and `c400001` are accepted and
  converted to `C400001`; anything else is rejected. Ranges:

  | Range | Area |
  |---|---|
  | 000000 | reserved: setup probe email (§9.6) |
  | 100000–199999 | tool unit tests |
  | 200000–299999 | harness unit tests |
  | 300000–399999 | API unit tests |
  | 400000–599999 | functional tests |
  | 600000–699999 | network-fault tests |

* **Run ID**: `<yyyymmddthhmmss>-w<worker>-<4 hex>`, e.g. `20261026t153012-w03-a1b2` (24 characters).
  The harness picks the timestamp and the 4 random hex characters once per run; the worker number
  distinguishes the run's parallel workers. Readable and searchable by time on purpose (no encoding).
* **Address**: `qa+C<number>_<run id>@example.com`, lower-case except the TestRail `C`, e.g.
  `qa+C100001_20261026t153012-w03-a1b2@example.com` (35-character local part). One builder
  (`EmiRun.address()`) enforces the TestRail case ID, a run ID in ASCII `[a-z0-9_-]` and a local part of
  at most 64 characters (RFC 5321); violations raise `API_ADDRESS_INVALID`. Non-ASCII input (e.g.
  Cyrillic, including a Cyrillic `С`) is rejected. The capital `C` is safe: Greenmail's `SEARCH TO` and
  the reader match recipients case-insensitively (Spike Evidence), and so does Gmail.
* **Extra recipients of one test** (batches over 5 or 10 addresses): `EmiRun.address(slot)` with slot 1 to
  99 gives `qa+C<number>_<run id>-rNN@example.com` (`addresses(n)` gives slots 1 to n). Every slot keeps the
  test's case ID and run, so its mail stays the test's own; the local part grows by 4 characters (39 for the
  example above).
* **Parametrized tests**: each case is its own TestRail case and carries its ID in its own marker
  (`pytest.param(..., marks=pytest.mark.emi(case="C410123"))`, built by `tests/functional/catalog.py`).
  The case marker wins over the function's and the class's; cases sharing an ID stop collection with
  `HARNESS_DUPLICATE_TEST_ID`.
* **Marker** (sender-generated mail only): headers `X-EMI-Test-Id: C<number>` / `X-EMI-Run-Id`, and a last
  body line `EMI-Test: C<number> run=<run_id>` in both parts (in HTML a small `<p>` after the footer, never
  near the CTA). Bodies stay valid UTF-8. Real-application mail has no marker; the unique address alone
  gives the same isolation.

## 5. `emi_reader`: the IMAP Reader

### 5.1 Transport (AMB-09)
Stdlib `imaplib` behind a small `ImapTransport` interface (connect, login, list, examine, status,
uid search, uid fetch, noop, logout). The wrapper keeps the original socket exception that `imaplib`
hides inside `IMAP4.abort` and holds one lock per connection. **Folder names** are Unicode `str` in our
API and settings; on the wire the wrapper encodes them in **modified UTF-7** (RFC 3501 §5.1.3) and
decodes names from `LIST` the same way, so a folder such as `Входящие` round-trips. Every IMAP server
accepts this encoding, Gmail and Greenmail included.
**Limitation (user decision 2026-10-26):** sending folder names as plain UTF-8 (`ENABLE UTF8=ACCEPT`,
RFC 6855) is not supported: Greenmail 2.1.15, the target server, does not advertise `UTF8=ACCEPT`
(Spike Evidence), so that path could not be verified.
Unit tests replace the transport with a simulation that fails on demand (§13).

### 5.2 Connections (AMB-28, AMB-47)
One IMAP connection per pytest-xdist worker (in service mode: per broker pool slot), reused by the tests
it runs; PRE steps only check it (`NOOP`, reconnect if needed). `max_connections` (default **10** per
run) caps connections; the harness never starts more workers than the cap (lowers `-n`, logs a warning).
Details and server limits: §11.

### 5.3 Search scope and folder accessibility (AMB-24, AMB-25, AMB-26)
* **Scope**: `[(account, [folders…])…]`, default `[(default, ["INBOX"])]`.
* **Probe** at session start and at each wait: `LOGIN → LIST → EXAMINE` per pair, classified
  `accessible`, `not_listed`, `noselect`, `select_denied`, `login_failed`, `network_error`.
  The **accessibility matrix** goes into every test's report (JUnit property, Allure attachment,
  HTML extras, log), pass, fail or skip.
* **Outcomes**: 0 accessible pairs → skip `READER_MAILBOX_NONE_ACCESSIBLE` (environment). ≥ 1 → run
  against the accessible pairs. The test's declared `expected_folder` inaccessible → skip
  `READER_MAILBOX_EXPECTED_FOLDER_INACCESSIBLE`.
* **Filter evidence** (any one): the folder is in `filter_risk_folders` and inaccessible; the folder is
  listed, `EXAMINE` is denied, but `STATUS UIDNEXT` grew during the wait; the folder carries `\Junk`
  (RFC 6154) and is inaccessible. Email not found + evidence → **environment failure**
  `READER_MAILBOX_FILTERED_TO_INACCESSIBLE` (skip, job stops; §9.4). Without evidence an inaccessible
  folder is only reported. Server filter rules (ManageSieve) are not read.

### 5.4 Waiting and matching (AMB-07, AMB-08, AMB-22, AMB-38)
* **Read-only**: `EXAMINE` and `BODY.PEEK[]` only; flags are never changed; `UNSEEN` is never relied on.
* **Algorithm**: on wait start, `UID SEARCH TO "<full address>"` finds pre-arrived mail; then each poll
  (default every **2 s ± jitter**) fetches headers of **new UIDs only** and matches them client-side.
  In service mode one poll loop serves every waiting request. A `UIDVALIDITY` change triggers a full
  re-search and an info log `READER_IMAP_UIDVALIDITY_CHANGED`.
* **Recipient match**: exact, case-insensitive, on the selected `match_fields` (**default `To` only**;
  any combination of `To`, `Cc`, `Bcc`, `Delivered-To`, `X-Original-To`), parsed with
  `email.utils.getaddresses`. Server search only narrows candidates (Greenmail matches exactly, Gmail by
  tokens, RFC 3501 by substring); the client-side check decides. The fields are a mailbox default
  (`WaitOptions`) and can be chosen **per wait** (`match_fields=` on every wait call, through `EmiApi`
  and the test broker), so one pooled connection serves `To`-only and `Cc` waits alike. A `Bcc`
  recipient is not visible in received headers; it is matched on `Delivered-To` (the sender stamps it,
  as Gmail does).
* **Match** = address + requested CTA + (sender-generated mail) the test/run marker. Emails without the
  CTA are not errors, just not matches.
* **Duplicates**: `wait_for_link` returns the newest match and logs `CASE_DUPLICATE_DELIVERY` when there
  are more. `wait_for_emails(count=N)` fails with `CASE_EMAIL_COUNT_EXCEEDED` if more than N match
  within the **settle window** (default 2 s) after the N-th arrives. The window can be set per wait
  (`settle_window=`); 0 suits mail fully delivered before the wait starts (extras are then already
  visible), which keeps a batch check over 10 addresses from costing 20 s.
* **Deadline**: default wait **60 s** → `CASE_EMAIL_NOT_RECEIVED`, unless the last attempt failed on the
  network, in which case that network keyword is primary (an outage is never reported as "email missing").

### 5.5 Retries (AMB-13)
Retry only what retrying can fix, only in the reader and the sender, inside the deadline, with capped
backoff 0.5 → 1 → 2 → 4 → 5 s: read timeout mid-session, connection dropped mid-session, socket-write
failure, "too many connections" (`READER_IMAP_CONNECTION_LIMIT`). **No retry** for connection refused,
DNS failure, connect timeout, authentication failure, TLS certificate error or protocol `BAD`: those fail
at once with the tool's keyword and count as infrastructure failures. The attempt history is attached.

### 5.6 MIME parsing and CTA extraction (AMB-12, AMB-17)
* Parse with `email` (policy `default`), decoding quoted-printable and base64 parts, RFC 2047 headers,
  `multipart/alternative` and `multipart/related` (both samples, Spike Evidence).
* **HTML part** with BeautifulSoup (`html.parser` backend, no lxml): the CTA is the `<a href>` whose
  normalized visible text (case-fold, whitespace collapsed, Unicode NFKC, nested `span` / `img[alt]`
  included) **equals** the CTA label. `class="btn"` is only a tie-breaker. `&amp;` is decoded;
  redirects are not followed.
* **Text part**: the URL whose path matches the CTA's `url_pattern` (e.g. `/activate`,
  `/invite/accept`); footer URLs are excluded by the pattern.
* **No HTML part** (plain text only): not an error; fall back to the text rule and log the secondary
  keyword `READER_PARSE_NO_HTML_PART`.
* **Both parts present and links differ** → fail `CASE_LINK_HTML_TEXT_MISMATCH` (a differing link may be
  phishing); an option downgrades this to a warning.
* Several distinct URLs at the winning step → `CASE_LINK_AMBIGUOUS`; none → `CASE_LINK_NOT_FOUND`;
  undecodable part → `READER_MIME_DECODE_FAILED`.
* **Pluggable strategy chain**: strategies register in order; `<form>` buttons and Outlook VML buttons
  are left out but can be added without API changes.
* **CTA registry**: `Cta.ACTIVATE_ACCOUNT`, `Cta.ACCEPT_INVITATION` built in; `Cta(label, url_pattern)`
  for new kinds (e.g. password reset).

### 5.7 Token helpers (AMB-39)
`token_subject(link) -> str` decodes the JWT in the link; `assert_link_for(link, address)` checks that
its `sub` equals the address (case-insensitive) and fails with `CASE_LINK_RECIPIENT_MISMATCH` otherwise.
Works for sender-generated and real-application mail; used by duplicate-delivery tests.

### 5.8 Mailbox inventory (AMB-40)
On connect and after each wait: whole-mailbox totals (`STATUS MESSAGES UIDNEXT`) and, **for the test's own
addresses only**, counts per category (`no_links`, `activation`, `invitation`, `unclassified`), before and
after. Always produced, attached to the report, and written as JSON records (run ID, test ID, timestamp,
totals, per-category before/after) in the run directory for cross-run statistical / ML analysis.
**Never** used for pass/fail.

## 6. `emi_sender`: the Sender (AMB-06, AMB-33 to AMB-37, AMB-39, AMB-45, AMB-51, AMB-61)

### 6.1 Templates and kinds
* JSON templates in `emi_sender/templates/`: `{"name", "kind", "source_eml"?, "subject"?, "text"?,
  "html"?, "link_template"?, "cta_label"?}`, placeholders `{recipient}`, `{token}`, `{display_name}`.
* Kinds: `activation` and `invitation` (built on `tests/fixtures/eml/*.eml`, byte-identical copies of the
  samples) and `generic` (built from scratch, **no links**).
* Per email the sender rewrites the recipient headers, `Message-ID`, `Date`, the token in the link (HTML
  **and** text part), adds the marker (§4), and **keeps each part's original transfer encoding**
  (decode → edit → re-encode quoted-printable / base64).

### 6.2 Tokens
Same structure as the samples: JWT header `{"alg":"HS256","typ":"JWT"}`; activation payload
`{sub, exp, jti}` with `exp` = `Date` + 24 h; invitation payload `{sub, site, jti}`. Signed with
HMAC-SHA256 and a **test-only** secret (configurable). `jti` (random) keeps two same-kind emails to the
same address in the same second distinct.

### 6.3 Batches and distributions
* Batch file: `{"recipients", "counts": {"generic", "activation", "invitation"}, "order", "concurrency",
  "seed", "delay_ms"}`; every CLI flag overrides its key.
* Up to **10 addresses** by default, extendable to **100**. Presets: `all_to_one`, `even_5`,
  `uneven_5` (weights 4, 3, 2, 1, 0), `one_each_10`; kinds and weights are assigned to addresses in
  **seeded random order**. An explicit per-address plan overrides the preset. The resolved plan is
  logged and written to the manifest.
* `--order fixed|random` + `--seed` (always logged, so any mix can be replayed), `--concurrency 1–32`
  (own SMTP connection each), `--jitter-ms`.
* Recipient field per email: `To` (default), `Cc`, `Bcc` or combinations. In Bcc tests the sender adds
  `Delivered-To: <plus address>` itself, emulating Gmail (documented as an emulation).

### 6.4 Delivery and routing
* `--transport smtp` (default): envelope `RCPT TO: qa@example.com` (the shared base mailbox), header
  `To: qa+<test id>_<run id>@example.com`. Greenmail does not fold plus addresses (Spike Evidence), so
  this is how every email lands in the one shared inbox; no relay is needed.
* `--transport append`: IMAP `APPEND` straight into `qa`'s INBOX (exact timing for pre-arrived and
  mid-wait cases).
* Default path is **through the test broker**; `--direct` bypasses it.
* Messages are always serialized with **CRLF line endings** (`policy.SMTP`) before SMTP or `APPEND`:
  `smtplib` dot-stuffs bytes without fixing bare LF, and Greenmail then stores a doubled dot
  (`cloud..example.com`), which corrupts quoted-printable links (Spike Evidence).

### 6.5 Input validation (AMB-37, AMB-49)
* Negative count → the sender sends **no** emails for it (count 0), logs the exception with
  `SENDER_NEGATIVE_COUNT`, continues. A unit test proves a negative number never reaches a send.
* Non-integer or out-of-range input → rejected (exit 2). Ranges: addresses 1–100, emails per kind
  0–100, total 0–1,000, concurrency 1–32, delay/jitter 0–60,000 ms, timeouts 1–3,600 s, ports 1–65,535.
* Total 0 after clamping → nothing sent, warning, exit 0.

### 6.6 Manifest and exit codes
Per email: address, field, kind, link, token `jti`, Message-ID, status, timing; returned by the API,
printed as JSON by the CLI, saved in the run directory. Exit codes and keywords:

| Exit | Meaning | Keyword(s) | Test outcome |
|---|---|---|---|
| 0 | all sent (including total 0) | n/a | n/a |
| 1 | some emails failed after retries | `SENDER_PARTIAL_FAILURE` + one `SENDER_SMTP_*` / `SENDER_IMAP_APPEND_FAILED` per cause | skip if every cause is an uncured network fault, else fail |
| 2 | invalid input | `CASE_CONDITION_INVALID_INPUT`, `CASE_CONDITION_NEGATIVE_COUNT` | skip (condition error) |
| 3 | server or service unreachable, port unavailable | `SENDER_SMTP_SERVER_UNREACHABLE`, `TEST_BROKER_UNAVAILABLE`, `TEST_BROKER_PORT_UNAVAILABLE` | skip; job stops |

## 7. `emi_test_broker`: the Shared Local Service (AMB-52 to AMB-54)

* **Process**: stdlib `ThreadingHTTPServer` on `127.0.0.1:<port>` (default **8025**,
  `--emi-test-broker-port` / `EMI_TEST_BROKER_PORT`), a per-start token file for local auth, no new
  dependencies. It owns every IMAP/SMTP connection for the machine, so `max_connections` is a real
  per-host budget across runs.
* **Endpoints**: `GET /health` (ready, version, config hash, pool usage, every setting and timeout with
  its source), `POST /send` (batch → manifest), `POST /wait` (address, CTA, marker, timeout → email /
  link; long-poll), `GET /inventory`, `POST /lease` / heartbeat / release, `POST /purge`.
* **Lifecycle**: `python -m emi_test_broker start|stop|status`; detached; idle shutdown after 30 min.
  **Auto-start**: a test's PRE step pings `/health`; if nothing answers it starts the broker once (binding
  the port is the lock), pings again, and skips with `TEST_BROKER_UNAVAILABLE` if still not ready.
* **Port rules**: port held by our own broker with the same version and config → reuse it. Held by
  something else, or binding fails → the broker exits and logs a timestamped "port could not be opened"
  entry (`TEST_BROKER_PORT_UNAVAILABLE`). Version/config mismatch → exit and log what was found
  (`TEST_BROKER_VERSION_MISMATCH`); no automatic port switching.
* **Log**: `logs/emi_test_broker_YYYY-MM-DD.log` (configurable, git-ignored), ISO 8601 local time with UTC
  offset; appended per date; if the file cannot be opened for appending, a new
  `emi_test_broker_YYYY-MM-DD_<n>.log` is created. **Debug reports** (`ci-debug-run` and local debug
  runs) include every entry for the run's time window plus all port-open failures of that date.
* **Run leases**: every run registers a lease, heartbeats every 30 s and releases it at the end; a lease
  expires after 5 min without a heartbeat. `inprocess` runs also register a lease when the broker is up.
* **Purge** (optional, manual maintenance only; nothing in the test flow calls it):
  `python -m emi_test_broker purge [--dry-run] [--older-than <duration>] [--all]`, implemented with IMAP
  `STORE +FLAGS \Deleted` + `EXPUNGE`. **Refused** while any lease or `/wait` is active
  (`TEST_BROKER_PURGE_REFUSED`, naming the active run IDs); **no override**. Default scope: only mail
  carrying the EMI marker. Logs counts per category and who ran it. Blind spots (other machines, people
  in a webmail UI) are documented.
* **Implementation notes** (Phase 3):
  * **State file** `<state dir>/broker_<port>.json` (pid, token, version, config hash); state dir
    defaults to the system temp folder (`--state-dir`). `GET /health` needs no token; every `POST`
    sends it in `X-EMI-Broker-Token`. The config hash covers every setting and a hash of the passwords.
  * **Server settings** (IMAP/SMTP hosts, ports, users, passwords) come from the `EMI_*` environment and
    `.env`; auto-start passes port, budget, log and state directories explicitly, global options before
    the `serve` subcommand. The spawned broker's stderr goes to `logs/emi_test_broker_start_errors.log`,
    so a broker that dies before its own log is open leaves a trace.
  * **Port lock**: `SO_EXCLUSIVEADDRUSE` on Windows (there `SO_REUSEADDR` would let two brokers share
    the port); `SO_REUSEADDR` on POSIX only skips TIME_WAIT.
  * **Connection budget**: requests queue for free slots; time queued counts against a wait's timeout;
    a wait that never gets a slot raises `READER_IMAP_CONNECTION_LIMIT` (source "broker budget").
    Waits reuse pooled reader connections; a failed wait's connection is closed so the next reconnects.
  * **Errors travel typed**: the broker answers 422 with the error's class, keywords and details, and
    the client raises the same class, so tests see identical errors in `service` and `inprocess` mode.
  * The client talks to the broker with `http.client`, not `urllib`, so `HTTP_PROXY` settings never
    route loopback calls through a proxy.
  * **Auto-start passes the caller's exact settings**: the client writes every setting except the
    passwords to `<state dir>/broker_<port>_settings.json` and starts the broker with `--settings-file`.
    The passwords go only into the child's environment. Before this, the child rebuilt its settings from
    `EMI_*` variables alone, so harness settings without a variable (folder list, log directory) gave it
    a different `config_hash`, and every run failed with `TEST_BROKER_VERSION_MISMATCH` (found in the
    first harness run, Spike Evidence).
  * **No console window on Windows**: the broker starts with `CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP`.
    `DETACHED_PROCESS` opened a visible console window, because a venv's `python.exe` is a launcher that
    starts the real interpreter, and a detached launcher's child gets a new console. With
    `CREATE_NO_WINDOW` both processes share one hidden console (Spike Evidence).
  * `GET /inventory` answers 501 until the reader's inventory (§5.8) exists. `--older-than` takes days
    (IMAP `BEFORE` is day-granular).

## 8. `emi_greenmail` and `emi_api`

### 8.1 `emi_greenmail` (AMB-16, AMB-42)
Readiness (`GET /api/service/readiness` → `{"message":"Service running"}`), IMAP/SMTP port probes,
configuration and user listing via the REST API. **Never starts Greenmail** (no autostart): locally the
user starts it (README); CI starts it in the workflow. Not ready → skip `GREENMAIL_NOT_READY` naming the
host:port probed.

* **Calls** (`GreenmailClient`, standard library only): `readiness()` (never raises), `ready()`,
  `check_ready()`, `wait_until_ready(timeout)` (polls every 0.5 s, for CI right after start),
  `configuration()`, `users()`, `create_user(email, login, password)` (for multi-account tests; the
  password is never logged), `delete_user(email_or_login)`, `purge()`.
* **Purge is destructive**: the shared mailbox keeps earlier runs on purpose, so only the purge tool calls
  it, behind the test broker's guard (`TEST_BROKER_PURGE_REFUSED` while runs are active).
  `POST /api/service/reset` (restart) is not exposed.
* A management call answered with an error raises `GREENMAIL_API_REQUEST_FAILED` (HTTP status and
  reason); an unreachable API raises `GREENMAIL_NOT_READY`.
* **Readiness keywords** (user decision 2026-10-26): API down → primary `GREENMAIL_NOT_READY` (the server
  is down), failed mail ports as secondary. API up but a mail port closed → primary
  `GREENMAIL_SMTP_CONNECTION_FAILURE` or `GREENMAIL_IMAP_CONNECTION_FAILURE` (TLS ports count as their
  protocol), secondary `GREENMAIL_NOT_READY`. All are environment failures (skip + stop).

### 8.2 `EmiApi` (AMB-38, AMB-42)
```python
api = EmiApi(settings)                     # resolves settings (§3), picks service / inprocess mode
run = api.start_run(test_id)               # -> EmiRun; registers the run lease
run.address()                              # -> str, §4
api.health()                               # -> Health (ready, versions, settings with sources)
api.sender.send(kind, address, count, ...) # -> Manifest
api.reader.wait_for_link(address, cta, timeout=None)                 # -> str
api.reader.wait_for_emails(address, cta=None, count=1, timeout=None) # -> list[ReceivedEmail]
api.reader.inventory(addresses)            # -> Inventory (§5.8)
api.greenmail.ready()                      # -> bool
```
`ReceivedEmail`: UID, folder, Message-ID, recipient, subject, arrival time, kind, links, marker.
Synchronous and blocking. Errors are typed (`EmiError` members) and each carries its primary and
secondary keywords. New test families register through a small plugin registry without core changes.

* **`EmiError` is a tuple** `(ReaderError, SenderError, TestBrokerError, GreenmailError, ApiError)`, used as
  `except EmiError:`. The tools cannot import `emi_api`, which rules out a shared base class. An ABC with
  `register()` does not work either, because `except` ignores virtual subclasses. A tuple is what
  `except` accepts. `keywords_of(error)` gives `(primary, secondary)` for any error (README).
* **Modes**: `service` → `BrokerClient`; `inprocess` → the same `BrokerService` request code called
  directly (`LocalBroker`). Both modes share the budget, pooling and error classes, and only HTTP and
  leases differ.
* **Run ID**: explicit argument > `EMI_RUN_ID` (shared by the run, made this worker's via
  `PYTEST_XDIST_WORKER`) > a new one. The first `start_run` registers the worker's lease (service mode),
  and `close()` / leaving `with EmiApi(...)` releases it.
* **Markers**: `api.sender.send` derives the marker from a test address. Reader waits match the exact
  address and require markers only when `test_id` / `run_id` are passed (real-application mail has no
  marker).
* **`health()` never raises**: it returns `Health(ready, problems, broker, settings)`, where each problem
  carries its keywords, so a PRE step can verify `.ready` and report `.keywords`.
* `start_run` takes the test ID in any accepted form and stores the TestRail case ID (`run.test_id ==
  "C400001"`). The reader compares marker test IDs in that form too, so `test_id="400001"` still matches.
* `api.reader.inventory` waits for §5.8 (RTM R-RD-11).
* **Observers**: `api.observe(callback)` is told `("manifest", <manifest JSON>)` after each send and
  returns a call that removes the callback. The harness uses it to attach manifests to the running test's
  report (§9.8) without the tests doing anything.

## 9. `emi_harness`: Runner, Steps and Gates

### 9.1 CLI (AMB-05, AMB-11)
`python -m emi_harness.cli` (argparse, generated from the option table):
`--test <name>`, `--file <list.txt>` (one name per line), `--keyword <KEYWORD or prefix>` (primary or
secondary), `--priority P0|P1|P2|P3|P4|P2-P4`, `--allure`, `--calibrate`, plus every `--emi-*` option. Without the
Allure CLI, `--allure` logs a warning and the run continues with JUnit XML + HTML (AMB-03).

### 9.2 Step model (AMB-46)
```python
def EMI_TOOL_400001_waits_for_activation_email(api: EmiApi, steps: Steps) -> None:
    run = api.start_run("C400001")                                                  # TestRail case
    with steps.precondition("Greenmail and test broker are running") as step:      # PRE
        step.verify("health OK", api.health().ready)
    with steps.step("Send 1 activation email", timeout=10) as step:                # S01
        manifest = api.sender.send("activation", address=run.address(), count=1)
        step.verify("manifest lists 1 email", len(manifest) == 1)
    with steps.step("Wait for the activation link") as step:                       # S02
        link = api.reader.wait_for_link(run.address(), Cta.ACTIVATE_ACCOUNT)
        step.verify("link matches the sent one", link == manifest[0].link)
```
* Numbering is automatic (`S01`, `S02`, …); `PRE` steps first; optional Step 0 via `steps.setup(...)`.
  Numbers are never negative or duplicated (invalid → skip with condition error).
* Each step logs START → action result → verification result → END (duration). An exception inside a
  step is caught, keyword-tagged and recorded against the step number.
* Failed `PRE` → **skip** (environment). Failed action or verification → test **fails** at that step;
  remaining steps → **SKIP**; **cleanup always runs** (`CASE_CLEANUP_FAILED` as a secondary keyword,
  never hiding the original failure).
* Per-step time limit default 30 s, overridable per step; per-test limit via `@emi_test(timeout=…)`.
* Steps go to JUnit properties, Allure steps and the HTML report.

### 9.3 Outcomes and keywords (AMB-04, AMB-25, AMB-44)
Every non-pass carries one **primary** keyword and any **secondary** keywords (catalog: `docs/keywords.md`).
There is no "not run" state: a test not executed because of an environment problem, an abort or the
circuit breaker is a **SKIP** with the triggering keyword and step number. **A skip is never a pass.**

### 9.4 Failing fast (AMB-44, AMB-49, AMB-59, AMB-65)
* **Infrastructure failure stops any job**: a failed PRE step (server or service unreachable, port not
  open) or a network fault that retries did not cure; remaining tests → SKIP.
* **Critical functional failure** (AMB-65): every P0 test is critical; a P0 failure is
  `JOB_CRITICAL_FUNCTIONAL_FAILURE` and fails the P0 job (and so the project status), but **does not
  stop it**. The P0 job finishes its other tests, and P1 and P2–P4 still run. Only the dependents of the
  failed test are skipped (§9.5), in P0, P1 and P2–P4.
* **Functional failures** are isolated (the job continues) in every job.
* **Circuit breaker** (P2–P4 and debug runs): 5 failures in a row with the **same** primary keyword →
  `JOB_CIRCUIT_BREAKER_OPEN` + that keyword; a pass or a different keyword resets the count.
* **Abort cascade**: lint or unit failure → nothing after runs; P0 or P1 job **abort** (infrastructure,
  circuit breaker, time budget) → lower jobs do not run. Test failures alone never stop the chain.

### 9.5 Dependencies (framework-rules §6, AMB-27, AMB-65)
A failed **or skipped** parent skips its dependents, transitively down the chain (a parent runs under the
same or milder conditions than its children). Each dependent's skip carries `CASE_DEPENDENCY_SKIPPED`
and names the test at the top of the chain and its keyword. A parent's result counts in its own job and
in every later job of the same run: the jobs share the run ID and read the earlier jobs' results from
`reports/<run>/`. In CI each job gets the earlier jobs' results of its chain before it runs.

### 9.6 Session setup and probe email (AMB-26, AMB-42, AMB-53)
Before any test: (1) Greenmail readiness and port probes; (2) test broker health (auto-start in service
mode); (3) folder accessibility probe; (4) **probe email**: one email per template type to
`qa+C000000_<run id>`, waited for 15 s (customizable) in the accessible folders. Any failure →
environment failure: `GREENMAIL_NOT_READY`, `TEST_BROKER_UNAVAILABLE`, `READER_MAILBOX_NONE_ACCESSIBLE`
or `HARNESS_PROBE_EMAIL_NOT_RECEIVED` (+ `READER_MAILBOX_FILTERED_TO_INACCESSIBLE` with evidence); every
test SKIP; job stops.

### 9.7 Priorities and downgrades (AMB-48, AMB-58)
* P0 = critical, P1 = high, P2–P4 = standard / low. P0 + P1 at most **20% of tests** and **20% of time**.
* Overflow: at collection the harness moves the excess **P1** tests to P2 for that run (declared `rank`
  first, then highest test ID), logs `JOB_PRIORITY_DOWNGRADED` per move and suggests a permanent re-label.
  P0 is never moved; P0 alone above 20% → `JOB_PRIORITY_P0_OVERFLOW`.
* **Gates** (`quality-gate`): P0 = 100%, P1 = 100%; P2–P4 together ≥ 85% (else fail, nothing else
  measured); (all passed) / (all functional) ≥ 95%. The gate prints both checks, the effective P2–P4
  floor and a keyword summary table. **All functional** means every test a job selected: each job lists
  its selection in `collection.json`, and a selected test without a result (a crashed worker, a runner
  killed at its timeout) counts as not passed, `JOB_RESULT_MISSING`. Without this, lost results shrank
  the measured set: a CI run whose P2-P4 job crashed after 748 tests passed the gate at 758/759.
  A job that never selected its tests (a usage error before pytest started) leaves no `collection.json`,
  so its tests are not even known; the gate is told which jobs were chosen (`--expect P2`) and fails,
  `JOB_RESULT_MISSING`, when a chosen job has no `collection.json`. It checks each operating system's
  directory on its own. Without this, a CI run whose P2, P3 and P4 jobs all exited on a usage error passed
  the gate on P0 and P1 alone, 10 of 1,016 tests.

### 9.8 Reports (AMB-03, AMB-04)
JUnit XML and HTML always (`pytest-html`), Allure optional (`allure-pytest`; HTML generation needs Java).
Per test: keywords, steps, settings with sources, accessibility matrix, inventory, manifest, attempt
history. Output in `reports/<run id>/`.

* **Layout**: `reports/<run>/<job>/` holds `junit.xml`, `report.html`, `results-<worker>.jsonl`,
  `settings.json`, `attachments/<case id>/NN_<name>.json` and, with `--allure`, `allure-results/`.
* **Settings with sources** are the same for every test of a job, so they are written once per job
  (`settings.json`, passwords shown as `***`) rather than repeated in each test's evidence.
* **Evidence** is collected per test (`emi_harness/evidence.py`). Sender manifests come through
  `EmiApi.observe` (§8.2), a tool error's `details` (attempt history, folder accessibility matrix,
  endpoint) are kept when the test fails, and a test adds its own with `steps.attach(name, data)`. Each item
  becomes a JSON file listed in the test's results line, a section of the HTML report and, with Allure, a
  JSON attachment.
* The raw email, the test broker's log entries for the test and the mailbox inventory (§5.8) are not yet
  attached (RTM R-HN-06).
* **Allure views** (AMB-69, `emi_harness/allure_report.py`). Allure's Categories tab classifies only failed,
  broken and skipped results, by status and a regex on the message, and never shows passed tests; it
  cannot read labels. So:
  * **Priority is a label** on every test: `parentSuite` P0 to P4 (the Suites tab groups every test by
    priority), `severity` blocker (P0), critical (P1), normal (P2), minor (P3), trivial (P4), and a `tag`.
  * **Failure cause is a category.** `categories.json` is written into every `allure-results` folder, one
    category per keyword family: Greenmail, test broker, reader, sender, public API, harness, job
    stopped, skipped because a parent did not pass, email not received, more emails than expected, link
    problems, verification failed, other test-case failures. Each keyword falls into exactly one.
  * At teardown the harness verdict sets the Allure result's status (`broken` for an infrastructure
    cause) and message `[KEYWORD] step Snn message (secondary: ...)`. Allure would otherwise record the
    exception text without the keyword, and no message at all for failures the harness decides itself
    (`CASE_NO_VERIFICATION`, `CASE_TIMEOUT`).

## 10. Time Budgets (AMB-22, AMB-49)

All customizable, reported by `/health` with their source, each mapped to a keyword:

| Budget | Default | Keyword when exceeded |
|---|---|---|
| IMAP / SMTP connect | 10 s | `READER_IMAP_CONNECTION_TIMEOUT` / `SENDER_SMTP_CONNECTION_TIMEOUT` |
| IMAP / SMTP read | 15 s | `READER_IMAP_READ_TIMEOUT` / `SENDER_SMTP_READ_TIMEOUT` |
| Send one email | 10 s | `SENDER_SEND_TIMEOUT` |
| Batch | 10 s + 0.05 s per email | `SENDER_BATCH_TIMEOUT` |
| Wait | 60 s (poll 2 s ± jitter) | `CASE_EMAIL_NOT_RECEIVED` |
| Settle window | 2 s | n/a |
| Probe email | 15 s | `HARNESS_PROBE_EMAIL_NOT_RECEIVED` |
| Step | 30 s, per step | `CASE_STEP_TIMEOUT` |
| Test | 90 s, per test | `CASE_TIMEOUT` |
| Jobs | P0 + P1 15 min total; P2–P4 60 min; debug 15 min; unit 10 min | `JOB_TIMEOUT` |
| Broker | idle shutdown 30 min; lease heartbeat 30 s, expiry 5 min | n/a |

Measured basis (Spike Evidence): SMTP ~98 emails/s sequential, ~192/s at concurrency 8; IMAP search and
fetch over 1,000 messages 0.06–0.15 s. `emi_harness.cli --calibrate` records p50/p95/p99 per operation and
suggests lower limits (e.g. p99 × 2); defaults are lowered on evidence.

## 11. IMAP Connection Limit (AMB-28, AMB-47, AMB-52)

* **Known limits**: Gmail about **15** simultaneous IMAP connections per account, shared by everything
  using it; Greenmail (local Docker, v2.1.15) **400** measured without failure (ceiling not reached).
* **Setting**: `max_connections` per run, **default 10** (`--emi-max-connections`,
  `EMI_MAX_CONNECTIONS`). One run stays under Gmail's 15; Greenmail fits 40 runs at the default.
* **One connection per worker**; the harness caps workers at `max_connections` (warning when `-n` is lowered).
* **Over-limit reply** (e.g. `[ALERT] Too many simultaneous connections`) is transient: jittered backoff,
  `READER_IMAP_CONNECTION_LIMIT`; if it persists, skip with that keyword (infrastructure).
* **Several runs or machines on one real mailbox** must divide the server's limit (3 runs on Gmail →
  `max_connections=5` each); they cannot coordinate otherwise.
* **Parallel runs** never see each other's mail (test ID + run ID in every address); no run purges while
  others may run; reports and logs go to per-run directories.
* **Poll jitter** keeps workers from polling in lockstep.

## 12. CI (AMB-15, AMB-43, AMB-44, AMB-48, AMB-55 to AMB-57, AMB-60)

User-facing details: `docs/ci_jobs.md`. Rule source: `.claude/rules/framework-rules.md` §1.

* **Triggers**: every push changing tests, tools, harness or their run configuration (requirements,
  `pyproject.toml`, `.pylintrc`, workflows), including the push a merge into `main` creates; every pull
  request into `main` with such changes (push and PR runs both run, AMB-60); documentation never
  triggers: the trigger lists name only code paths, and a final `!**/*.md` excludes Markdown even inside
  them. **Every workflow has `workflow_dispatch`**, so any job can be started by hand.
* **Regression run `ci-regression-run`** (project status; named `joint-ci-workflow` until 2026-10-27,
  AMB-66). Started by hand, it takes one tick box per
  functional job (P0, P1, P2, P3, P4; all ticked by default, AMB-66, AMB-67): `lint` and `unit` always run, then the
  ticked jobs; a job left out neither runs nor stops later ones, and `quality-gate` measures the results
  that exist (it does not run when no functional job is ticked). Pushes and pull requests run every job.
  1. `lint` (Ubuntu): pylint 10.00/10.
  2. `unit` (Windows + Ubuntu): every unit-test folder, including the network-fault layers 1 and 2 (§13).
     Mandatory gate.
  3. `functional-p0` → `functional-p1` → `functional-p2`, `functional-p3`, `functional-p4` (the last three
     side by side, a matrix, AMB-67), a chain **per OS** (Windows and Ubuntu side by side; both must
     pass). The chain stops only when a job aborts; test failures, P0 included, do not stop it (AMB-65).
     The jobs share one run ID (chosen by the chain's first job, `run-id`), and the later jobs first get
     the P0 and P1 results, so dependents of a failed parent are skipped in later jobs too. A parent has
     the child's priority or a higher one (`HARNESS_DEPENDENCY_ORDER` at collection otherwise). The three
     low jobs do not see each other's results: a P3 or P4 test whose P2 parent failed runs and fails
     instead of being skipped, accepted because waiting for P2 costs more. Each low job fails only when
     it aborts; their 85% together is measured by `quality-gate`. Locally,
     `--priority P2-P4` still runs the three as one job, which itself needs 85%.
  4. `quality-gate` (Ubuntu): the gates of §9.7 over both OSes' results; builds the Allure report once.
* **Functional job steps**: checkout → `actions/setup-python` (3.14) → `pip install -r requirements.txt
  -r requirements-dev.txt` → start Greenmail → readiness poll → start `emi_test_broker` → session setup
  and probe email (§9.6) → `python -m emi_harness.cli --priority <slice>` → upload reports and the broker log.
* **Greenmail per OS** (`.github/actions/start-greenmail`): Ubuntu starts `greenmail/standalone:2.1.15`
  with `docker run`, using the README's options. That way one composite action serves both OSes; a
  `services:` container cannot exist on a Windows job. GitHub's Windows runners cannot run Linux containers, so Windows runs the
  `greenmail-standalone-2.1.15.jar` (cached) on Temurin 17 via `actions/setup-java`, in the background.
  It is started through WMI (`Win32_Process.Create`), not as a child of the step: on Windows runners a
  step's children end with the step (the JVM vanished mid-start, seen in CI on 2026-10-27).
  Reason for Windows functional CI: Java is needed anyway (Allure's report generator) and ships with the
  runners, and the behaviour that differs most on Windows (broker detached auto-start, port binding,
  shared log-file locking, real socket errors) cannot be covered by unit-test simulations.
* **Outside project status**: `ci-debug-run` (manual; test list, file or keyword; includes the broker log
  for the run window and that day's port failures; can select the `docker` marker for layer-3 fault
  tests) and one standalone unit job per tool (`unit-emi-reader`, `unit-emi-sender`,
  `unit-emi-test-broker`, `unit-emi-greenmail`, `unit-emi-harness`), each triggered by its own tool or
  unit-test folder and by hand; they never block.
* **Overlapping runs**: PR / feature-branch runs cancel the older run of the same branch; `main` and
  manual runs are never cancelled and may run in parallel; a job using a shared real mailbox queues.
* **Test validity** (§13.4, AMB-64): `test-validity` (manual and weekly, Ubuntu) runs the named-fault
  and stability checks; it never blocks. Weekly it runs every fault and repeats P0 and P1 three times (a
  full-suite stability run would take hours every week); by hand any fault, job and repeat count.
* **Workflow files**: `.github/workflows/ci-regression-run.yml` (project status), `functional-chain.yml` (the P0 → P1 →
  P2 / P3 / P4 chain, called once per OS), `ci-debug-run.yml`, `unit-standalone.yml` (a `changes` job lists the
  tools whose folders changed; each tool's job calls `unit-tool.yml`), `test-validity.yml`. Shared steps
  are composite actions: `.github/actions/harness-job` (Python, requirements, Greenmail, broker, one
  harness command, uploads; its exit code tells the chain an abort from failures) and `start-greenmail`.
* **Allure on GitHub Pages**: after a run on `main`, `quality-gate` builds one Allure report from both OSes
  and `allure-pages` deploys it. This needs Pages set to "GitHub Actions" in the repository settings;
  until then that job fails without affecting project status.
* **Secrets**: `EMI_GREENMAIL_PASSWORD` (optional) sets the Greenmail user's password; without it the
  public test password `password` is used.

## 13. Test Strategy

### 13.1 Unit tests (`unittests/emi_*`, no server needed)
Per tool: settings and precedence, option-table parity, secret redaction, address and run-ID builder
(ASCII, length, Cyrillic rejected), MIME decoding of both samples, every extraction strategy, HTML/text
mismatch, token helpers, accessibility classification (including `noselect` / `select_denied`, which
Greenmail cannot produce), retry classification, sender clamping and validation, template rewriting
(encodings preserved), broker port / lease / purge-refusal / log-rotation rules, step model, gates,
downgrades, circuit breaker, dependency cascade, keyword-catalog completeness, duplicate test IDs.

### 13.2 Network faults (AMB-14), inside each tool's unit tests
1. **Simulated transport**: refuse, connect timeout, read timeout, drop mid-reply, socket-write error,
   "too many connections", `BAD`. Checks outcome, keywords, host:port, attempt history, no password shown,
   and that only healable faults are retried. Runs in CI (`unit`, Windows + Ubuntu).
2. **Fault proxy**: an in-repo pure-Python TCP proxy in front of a small in-process simulated IMAP/SMTP
   server, modes `pass`, `latency`, `blackhole`, `reset_after_bytes`, `close_on_client_write` (Windows
   10053/10054 and Linux `BrokenPipe` / `ConnectionReset`), `refuse`. Runs in CI (`unit`, both OSes).
3. **Real outage**: stop / restart the Greenmail container mid-test, marker `docker`; local, or CI only
   when started by hand. Skipped elsewhere with `HARNESS_DOCKER_UNAVAILABLE`.

Expected faults are passes when handled correctly; they never trigger the infrastructure job-stop rule
or the circuit breaker.

### 13.3 Functional tests (`tests/`, against Greenmail)
* Delivery: SMTP send per CTA kind (activation, invitation), `APPEND`, pre-arrived mail, mail arriving
  mid-wait, resends and duplicates, count exceeded.
* Recipient fields: `To` (default), `Cc`, `Bcc` (with `Delivered-To`), `Delivered-To`, `X-Original-To`,
  each alone, negative tests (not matched under `To`-only), combinations.
* Batches: the four distribution presets with mixed kinds, random order, concurrency, seeds.
* Isolation: parallel runs, other runs' mail, unmarked manual mail ignored.
* Accessibility: missing folder, second account with bad credentials, unreachable host, mixed pairs.
* Links: label match, text fallback, plain-text-only email, HTML/text mismatch (fail and warn modes),
  recipient mismatch via `sub`.
* **Scale**: parametrized families (kinds × fields × distributions × orders × transports) reach
  **1,000+** test cases with unique IDs in 400000–599999, P0 + P1 within 20%.
* **Critical (P0)** (AMB-68): one email of each kind, sent and received: an activation email yields its
  link, an invitation email yields its link, and a generic email yields no link (neither CTA can be
  extracted, `CASE_LINK_NOT_FOUND`). Per link kind, a wait for that link with two non-matching emails
  already in the mailbox returns only the matching email sent 2 s into the wait. If any of these fails,
  nothing else the tool does can be trusted.
* **P2, P3 and P4 by seed** (AMB-70): every batch preset × order × field × kind mix × concurrency runs
  with seeds 1-2 in P2, 3-4 in P3 and 5-6 in P4. The three jobs run side by side, each with its own
  Greenmail, and a wait's search covers the whole mailbox, so a job that sends more mail slows its own
  later tests; with the split each job sends about the same mail (P2 406 tests, P3 311, P4 288).

### 13.4 Test validity (AMB-64)
A test is only worth its result if it fails when the tool is broken (no **false positive**) and passes when
the tool works (no **false negative**). Three checks show this for the functional suite.

**1. Named faults (false positives).** `emi_harness/faults.py` holds a catalog of faults. A fault is a
`MailBackend` wrapper given to `EmiApi` through its `backend` argument, so the tools' own code has no fault
flags. It is selected with `--emi-fault <name>` (`EMI_FAULT`), works in both client modes, and is
recorded in `settings.json` and in every results line. Each fault names the tests that must catch it
and the keyword they must fail with:

| Fault | What breaks | Must be caught by | Expected keyword |
|---|---|---|---|
| `sender-drops-email` | `send` and `send_batch` deliver one email fewer than asked; the manifest still lists them all | delivery 400001, 400002, 400006–400008, counts, counts exceeded, markers, arrival during the wait | `CASE_EMAIL_NOT_RECEIVED`; counts exceeded: `CASE_VERIFICATION_FAILED` (the expected error is not raised) |
| `sender-ignores-field` | a batch puts the address in `To`, whatever field was asked | counts and batches in `Cc` / `Bcc`; fields not matched (`cc` / `bcc` sent) | `CASE_EMAIL_NOT_RECEIVED`; fields: `CASE_VERIFICATION_FAILED` |
| `reader-any-field` | a wait matches the address in every recipient field, whatever `match_fields` says | fields not matched | `CASE_VERIFICATION_FAILED` |
| `reader-ignores-markers` | a wait drops its test and run markers | other run's marker not matched | `CASE_VERIFICATION_FAILED` |
| `reader-slot-collapse` | a wait for a slot address `<run>-rNN` reads slot 0 instead | batches over 5 or 10 addresses; isolation (own extra slot) | `CASE_EMAIL_NOT_RECEIVED`, `CASE_EMAIL_COUNT_EXCEEDED` or `CASE_VERIFICATION_FAILED` |
| `reader-duplicates-email` | a wait returns one email twice in place of another (same count) | counts of 2 or more, batches, delivery 400004 | `CASE_VERIFICATION_FAILED` |
| `reader-returns-extra` | a wait returns one email more than asked | counts, batches (they compare received and sent emails as lists, not sets) | `CASE_VERIFICATION_FAILED` |
| `reader-wrong-link` | the link returned differs from the email's link in one character | delivery 400001, 400002, 400007, 400008, counts | `CASE_VERIFICATION_FAILED` |
| `reader-gives-up-early` | a wait gives up after 1 s, whatever its timeout | arrival during the wait (2 s or later) | `CASE_EMAIL_NOT_RECEIVED` |

`python -m emi_harness.validity faults [--fault <name>]` runs each fault's catching tests under that fault
and checks that every one of them **fails with an expected keyword**. A catching test that passes, is
skipped, or fails with another keyword is a **gap**: the report names the fault, the test, its status and
keyword; the command exits 1. Gaps are fixed in the tests (a missing check), never by weakening the fault.
The catalog grows with the suite: a new functional family adds the faults it catches.

**2. Stability (false negatives).** `python -m emi_harness.validity stability --repeat N` (default 3) runs a
selection (`--test`, `--file`, `--keyword`, `--priority`, `-n`) N times against a healthy Greenmail, each
repeat with its own run ID. A test that does not pass in every repeat is listed as **flaky** (it passed
in some repeats) or **failing** (in none), with its keywords per repeat. **Timing margins**: a test whose
longest duration over the repeats is above 80% of its test limit, or a step above 80% of its step
limit, is listed as a **thin margin**, so a limit too close to reality is found before it fails at random.
Exit 1 when anything is listed.

**3. No test passes by checking nothing.** The harness counts every `step.verify` and every passing plain
`assert` (pytest's `enable_assertion_pass_hook`). A functional test that would pass with a count of 0
fails with **`CASE_NO_VERIFICATION`**, in every run, not only validity runs. `pytest.raises` alone does not
count: the test states what it checked about the error with `step.verify`. A `pytest.raises` that is not
satisfied (pytest's `Failed`, "DID NOT RAISE") is a failed check, `CASE_VERIFICATION_FAILED`.

**Guards and outputs.** Results produced under a fault are never gate results: `emi_harness.gate` refuses
them (exit 2). A fault run turns the circuit breaker off (its failures are the point), and every fault
leaves the session probe's calls alone, so the setup check cannot skip the run. The catch table lives with
the suite in `tests/functional/validity.py`. Reports go to `reports/validity/<timestamp>/` (`faults.json`
or `stability.json`), and each underlying job keeps its usual reports under its own run ID. **Where**: locally, and in the non-blocking workflow `test-validity.yml`
(manual, with a choice of check, fault and selection; weekly on a schedule; Ubuntu with Greenmail as a
service container). It never affects project status.

**Scope.** These checks cover the functional suite. Unit tests use Sims whose faults are asserted
directly (§13.2), and are checked by review.

## 14. Dependencies and Project Files (AMB-21, AMB-43)

* `requirements.txt` (runtime, pinned exactly): `beautifulsoup4`, `python-dotenv`.
* `requirements-dev.txt` (pinned exactly): `pytest`, `pytest-xdist`, `pytest-html`, `pylint`,
  `allure-pytest`.
* `pyproject.toml`: `requires-python >=3.14`, pytest collection settings (§4), tool configuration.
* `.pylintrc`: `variable-rgx`, `argument-rgx`, `attr-rgx` = `[a-z_][a-z0-9_]{2,}$`, empty `good-names`
  (`id` allowed as an attribute only, code-style §1); relaxed `function-rgx` for test folders.
* `.env.example` (names only), `conftest.py`, `CLAUDE_LOG.md`, `.gitattributes` (`*.eml -text`, already
  present), `.gitignore` (`.env`, `logs/`, `reports/`, `.claude/logs/`, `.venv/`, `.idea/`).

## 15. Left Out (documented in the README)

Separate repositories for the tools, the harness and the test cases (§2; one repository because of the
one-day time limit).
Gmail-specific features (`XOAUTH2`, `X-GM-RAW`); IMAP `IDLE` (polling only); `<form>` and Outlook VML
buttons (strategy chain is extensible); following redirects; reading server filter rules (ManageSieve);
several servers in parallel (designed for, implemented 1 : 1); starting Greenmail from tests; a
plus-folding relay for third-party applications; whole-mailbox classification; coordinating one
connection budget across machines; async APIs; UTF-8 folder names via `UTF8=ACCEPT` (Greenmail does not
support it; modified UTF-7 is used, §5.1).

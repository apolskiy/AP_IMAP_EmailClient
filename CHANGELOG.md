# Change Log

What changed in each commit, newest first. Each entry says what was added, changed or fixed and why,
and names the CI run that found a problem when one did. Register rows (AMB-xx) are in
[docs/design/phase0_email_client_ambiguities.md](docs/design/phase0_email_client_ambiguities.md); the
reasoning behind each decision is in [CLAUDE_LOG.md](CLAUDE_LOG.md).

## Unreleased

### Changed
* **The RTM records the `test-validity` rerun (R-TV-02, R-TV-04).** Run 40583543917 caught all 9 named
  faults, and its stability check passed 3 repeats of all 1,034 functional tests with no findings, each
  repeat in about 11 minutes. Run 40451473560's second repeat had timed out at about 15,600 messages in
  INBOX; with each job deleting its own mail, INBOX held at most 3,429. R-TV-04 moved from Partial to Done.

## 9d14db8 (2026-10-27)

### Added
* **A run can delete its own mail and nothing else (AMB-79).** `python -m emi_test_broker purge --run-id
  <run>`, `EmiApi.delete_run_mail()` and the harness option `--emi-delete-own-mail on` delete the mail of
  one run, every worker's, and keep every other run's. It is refused only while that run is active, so it
  is safe next to other runs. `test-validity` run 40451473560 had filled Greenmail's INBOX to about 15,600
  messages, and its second stability repeat timed out.
* **One shared poller serves every wait in the test broker (AMB-80).** One IMAP connection searches for
  all waiting addresses at once and fetches the matches in one command per cycle, so a wait no longer
  takes a connection or a fetch of its own. On Greenmail every fetch costs more as the folder fills
  (about 6 s at 15,000 messages), and per-wait polling made the batch tests take 160-213 s against their
  90 s step limit; with the poller a 10-email batch wait took 13.5 s at the same size. Broker version
  1.2.0.
* **One option table for every tool (AMB-81), in the new package `emi_config/`.** Each tool's
  `settings.py` declares its settings once, and the sender, test broker and harness command lines and
  the pytest options are generated from those rows, so a setting has one name, one `EMI_*` variable and
  one default everywhere, and each tool works on its own outside the tests. New unit job
  `unit-emi-config`; a change to `emi_config/` runs every tool's job.
* **The broker's `/health` shows where each setting came from** (argument, command line, environment,
  `.env` or default), so a wrong value can be traced without the harness's `settings.json` (R-CFG-04).
* **Recipient field combinations (AMB-82).** One email can carry the address in several fields
  (`--field to,cc` on the sender, `recipient_field=(Field.TO, Field.CC)` in a batch); the manifest names
  them as `to+cc`. Broker version 1.3.0.
* **15 functional cases (AMB-83)**: every combination of `To`, `Cc` and `Bcc` for each email kind, found
  on each of its fields and missed on a field outside it, and an address in `Delivered-To` alone. Live
  against Greenmail all 24 recipient-field cases pass, and the two field faults are caught by all 30 of
  their catching tests.
* **`--via-broker` on the sender (AMB-84)** sends through the shared test broker, starting it if needed;
  without it the sender sends directly and does not need the broker.
* **`expected_folder` on the reader's waits (AMB-85)**, through `EmiApi` and the broker: a test that
  expects mail in a folder it cannot read (for example `Spam`) is skipped at once with
  `READER_MAILBOX_EXPECTED_FOLDER_INACCESSIBLE` instead of waiting out its timeout.

### Changed
* **The stability check runs P0, P1, P2, P3 and P4 as separate jobs (AMB-79)**, one after another under
  one run ID, each deleting its own mail, so the mailbox never grows across the whole suite.
* **Renamed the sender's and broker's command-line flags** to the option table's names: `--host` is
  `--smtp-host`, `--security` is `--smtp-security`, `--transport` is `--sender-transport`, and the
  broker's `--port` is `--test-broker-port`. The old names were the tools' own, and differed from the
  harness and the `EMI_*` variables.
* **Closed the remaining design gaps that will not be built (AMB-86)**: a connection per worker in
  `inprocess` mode (the shared poller replaced it), several mail accounts, a generated
  `docs/keywords.md`, a queue for jobs sharing Gmail, and the functional cases that the unit tests
  cover or the sender cannot produce. The README's "Designed but Not Yet Implemented" section became
  part of "What Was Left Out", and the README gained a section on using the sender and the broker on
  their own.
* README test counts (479 unit tests, 66 network tests, 1,034 functional tests), design §5.3, §5.9 and
  §13.3, CLAUDE.md's folder list, `docs/ci_jobs.md`, and RTM rows R-CFG-02, R-CFG-04, R-KW-01, R-RD-03,
  R-RD-04, R-RD-05, R-SD-04, R-SD-06, R-TB-02, R-TB-08, R-API-01, R-CI-01, R-TS-01, R-TS-03, R-TV-01,
  R-TV-02 and R-TV-04.

## a65e11a (2026-10-26)

### Changed
* **README lists the features that are designed but not yet implemented.** A new section names each
  one (shared poll loop, a connection per worker in `inprocess` mode, several accounts, recipient field
  combinations, the sender CLI through the broker, one option table for every CLI, setting sources in
  `/health`, a generated keywords doc, the Gmail queue, and the functional cases that need them), what
  the code does today, and its RTM row. They had been visible only in the RTM.

## 6cf882d (2026-10-26)

### Fixed
* **The session probe's manifest was attached to the first test.** The probe email is sent while the
  first test is being set up, so its manifest appeared in that test's evidence (seen as a second
  `manifest` under C401009). Evidence now goes to the test it names (a manifest by its test ID, an
  inventory by its address); the probe's items are attached only to tests that session setup skipped,
  where they explain the skip.
* **Design and RTM corrections.** They said `ci-debug-run` can select the outage tests; those run in
  `unit-outage`. The inventory design said an undecodable email is `unclassified`; the parser never
  fails, so every fetched email gets a category. The RTM records that `test-validity`'s faults check
  passed on GitHub (run 40451473560).

## be46ae5 (2026-10-26)

### Changed
* **A wait rides through a mail server restart** (AMB-78). The new outage tests showed that a wait
  running while Greenmail restarted ended at once: the dropped connection was retried, but the
  reconnect was refused, and a refused connect was never retried. Once a wait has had a working
  connection, a refused reconnect is now retried with the usual backoff inside the deadline, and the
  wait finds the email sent after the restart. A refused first connect still fails at once; a server
  that stays down ends the wait at its deadline with `READER_IMAP_CONNECTION_REFUSED`. 2 new reader
  tests; outage test 100133 now checks the ride-through.

### Fixed
* **The fault proxy's `refuse` mode let connections through on Linux.** In run 40450901295 the two
  refused-port tests (100128, 110066) failed on Ubuntu only: a socket closed while another thread waits
  in `accept` keeps listening on Linux. The proxy now shuts the listener down before closing it and
  polls for connections. Checked in a Linux Python 3.14 container (all 451 unit tests pass). The
  simulated servers no longer print a traceback for each connection the proxy breaks on purpose.

## 6c56879 (2026-10-26)

### Added
* **Mailbox inventory from every wait** (AMB-76). A report showed what a test found but not what else
  was in the mailbox, so a slow or crowded mailbox could not be told apart from a missing email, and
  nothing could be compared across runs. Each wait now records the folder totals (messages, `UIDNEXT`)
  and the emails at the test's address by kind (`activation`, `invitation`, `no_links`,
  `unclassified`), when it starts and when it ends. It costs no extra IMAP command: the totals come from
  the wait's own `EXAMINE` and the kinds from the emails it fetches anyway, since a separate lookup costs
  about 0.7 s per fetch at 5,000 messages (AMB-71). The inventory goes from the reader through the test
  broker (version 1.1.0) and `EmiApi`'s observers to the harness, which attaches it to the test and
  appends one line per wait to `inventory-<worker>.jsonl` in the job folder. A failed wait's error
  carries it too. The session probe's wait is logged under `C000000`, not under the first test. Never
  used for pass/fail. `GET /inventory` stays unbuilt.
* **Network faults on real connections** (AMB-77, layer 2). The network unit tests raised simulated
  exceptions, so nothing showed what `imaplib` and `smtplib` actually raise when a connection breaks
  on Windows or Linux. A fault proxy (`unittests/netfaults/`) now sits between the real client and a
  small simulated IMAP or SMTP server and slows, silences, resets or refuses the connection. 11 new
  reader and sender tests (`network`) check the keyword and retry decision of each, and that a reset
  mid-session heals on a new connection with the email delivered or found once. They run in every unit
  job on both systems; a change to `unittests/netfaults/` starts the reader and sender standalone jobs.
* **Greenmail outage tests** (AMB-77, layer 3). 4 tests (`unittests/outage/`, marker `docker`) stop
  and restart the real Greenmail container under the reader, the sender and the API. Each must report
  the outage with a network keyword, never as an email not received, and `api.health()` must say
  `GREENMAIL_NOT_READY` and recover. The default unit run leaves them out, so the regression never
  stops Greenmail; the new `unit-outage` workflow runs them on Ubuntu weekly, by hand and when they
  change. They found that a wait does not ride through a restart, recorded as open question AMB-78.
* The README counts 1,472 tests: 453 unit (61 network, 4 outage) and 1,019 functional.

### Changed
* **P0 + P1 time share is a 30% warning** (AMB-75). The gate printed the share against a 20% target that
  nothing enforced. The 20% ceiling on the number of P0 and P1 tests stays as it was. For time, P0 + P1
  should take at most 30% of all test time, leaving 70% for the rest; above it the gate prints
  `JOB_PRIORITY_TIME_SHARE` in the log and on the run page, and still passes, because timing fails a test
  only through that test's own time limit. A run without P2-P4 results is not measured, since P0 and P1
  alone are always 100%.

## 5f9bd6f (2026-10-26)

### Added
* **A failed or skipped test's emails and broker log lines in its report** (AMB-74). A failure's evidence
  had manifests and error details but not the emails themselves, and CI discards the mailbox, so a wrong
  link or a missing marker could not be checked afterwards. After such a test the harness now asks the
  reader, through the test broker's pooled connection (new `POST /emails`, `api.reader.emails_to`), for
  every email at the test's addresses, matched or not, newest 30. Each becomes an `.eml` attachment (cut
  at 256 KB). The test broker's log lines of the test's window that name it become a `.log` attachment.
  Both appear as files, in `report.html` and in Allure. Passing tests are unchanged. Collecting never
  changes a result; a failure to collect is noted instead.

### Changed
* **README summary names every tool.** It spoke of one "tool"; it now lists the reader, sender, test
  broker and Greenmail client with their folders, then the `EmiApi` library and the harness. The test
  counts are updated for the 8 new unit tests (1,450 in total, 431 unit).

## 861e03b (2026-10-26)

### Added
* **Job counts in the CI log and on the run page** (AMB-72). A job's log said only that it passed, and the
  run page showed a green or red mark; the counts were only in `report.html`. Every functional job now ends
  with a line such as `P2 job: 406 selected, 400 passed, 4 failed, 2 skipped, 0 without a result`, taken
  from the harness results so it agrees with the gates. In GitHub Actions each job also writes the counts
  and up to 50 tests that did not pass (keyword, step, message) to its step summary, titled with the job
  and operating system, and `quality-gate` writes its per-priority report there. Harness unit tests clear
  `GITHUB_STEP_SUMMARY`, so they never write to a real run page.
* **Test counts in the README** (AMB-73). The README gave only the functional total. It now counts unit
  tests per tool with their network tests, functional tests per family and priority, and the totals:
  1,442 tests, of which 423 are unit tests (50 network) and 1,019 functional. Functional tests are counted
  by family because each one drives the sender, test broker and reader together.
* **`network` marker on the network-fault unit tests** (AMB-73). Nothing marked them, so they could be
  neither counted nor run alone. The 28 tests that make a connection fail (refused, closed port, port
  taken, dropped or reset, timed out, connection limit, server down) and check the tool's keyword, retry
  decision or exit code now carry it; `python -m pytest unittests -m network` runs them.

## 7fdcc54 (2026-10-26)

### Changed
* **Waits fetch once per look instead of once per message** (AMB-71). In CI the same test took longer
  the later it ran in a job (P4: 4.4 s to 7.2 s per test). Measured on Greenmail, a `UID SEARCH` costs
  about 45 ms at any folder size, but every `UID FETCH` command costs a fixed amount that grows with the
  folder (about 0.7 s at 5,000 messages). A wait used to fetch the headers of every new message, other
  tests' mail included, and then each match on its own. It now searches `UID <next>:*` for its address
  and fetches every candidate whole in one command. A poll that finds nothing costs no fetch. Measured
  back to back on a 5,054-message folder, one single-email wait went from 1.84-1.99 s to 1.07-1.12 s.
  Limiting only the first search to recent UIDs was measured too and dropped, since it gained nothing.

## 25b5779 (2026-10-26)

### Changed
* **Documentation never triggers CI.** The trigger lists already named only code paths, so the change
  log push started nothing; a Markdown file inside a listed folder (a tool's README, a note under
  `.github/`) would still have started a full regression. Both `ci-regression-run` and
  `unit-standalone` now end their path lists with `!**/*.md`.
* **`joint-ci-workflow` renamed `ci-regression-run`** (AMB-66 revised): the name says what it runs and
  pairs with `ci-debug-run`. GitHub lists the earlier runs under the old name.

## bd67917 (2026-10-26)

### Added
* **`CHANGELOG.md`**, covering every commit since the first one.
* **Critical (P0) tests for one email of each kind** (AMB-68). The P0 report listed one test, because
  only C400001 (an activation link) was P0. P0 now has 5 tests: C400001 (activation link), C400002
  (invitation link, moved from P2), the new C400006 (a generic email arrives and neither link can be
  extracted from it, `CASE_LINK_NOT_FOUND`), and the new C400007 and C400008 (a wait for one kind of
  link skips two non-matching emails already in the mailbox and returns only the matching email sent
  2 s later). The suite has 1,019 tests. All five catch `sender-drops-email`, and those with a link
  catch `reader-wrong-link`.
* **Allure priorities and categories** (AMB-69). The published report's Categories tab was empty and
  "unmapped", and nothing showed tests by priority. Allure categories only classify tests that did not
  pass, by a pattern on the message, so priorities became labels instead: the Suites tab groups every
  test under P0 to P4, and severity runs from blocker (P0) to trivial (P4). Categories now group the
  tests that did not pass by keyword family (Greenmail, test broker, reader, sender, API, harness, job
  stopped, parent did not pass, email not received, too many emails, link problems, verification
  failed, other). The harness writes each message as `[KEYWORD] step Snn message`, which Allure
  otherwise recorded without the keyword, or not at all for failures the harness decides itself.

### Changed
* **P2, P3 and P4 split batches by seed, not by preset** (AMB-70). In run 40437111627 the three
  side-by-side jobs took about 17, 4 and 5 minutes: P2 held two of the four batch presets and sent
  about twice the mail of P3 or P4. Each job has its own Greenmail and every wait searches the whole
  mailbox, so per-test time grows as a job's mailbox fills, and the busiest job slows the most. Seeds
  1-2 now run in P2, 3-4 in P3 and 5-6 in P4 for every preset, so the jobs hold 406, 311 and 288
  tests and send about the same mail.

## aaf70c1 (2026-10-26)

### Fixed
* **The quality gate passed a run in which most tests never ran.** In run 40436448076 the P2, P3 and
  P4 jobs exited on a usage error before selecting any test, left no `collection.json`, and the gate
  measured P0 and P1 alone: 10 of 1,016 tests, all passed. The gate is now told which jobs were chosen
  (`--expect P2`) and fails with `JOB_RESULT_MISSING` when a chosen job left no collection, checking
  each operating system's reports separately. Replayed on that run's artifacts, the old call passed
  and the new one fails, naming all six jobs.

## 278487a (2026-10-26)

### Fixed
* **`--priority` is case-insensitive.** In run 40436448076 all six P2, P3 and P4 jobs failed at once:
  the CI matrix passed `p3`, and the CLI accepted only `P3`.

### Added
* **`HARNESS_DEPENDENCY_ORDER`.** A test may depend only on tests of the same or a higher priority;
  otherwise collection fails. P3 and P4 tests whose parent is a P2 test run alongside P2 and fail
  quickly rather than being skipped, which is faster than waiting for P2 to finish.

## 23966b3 (2026-10-26)

### Added
* **The `test-validity` workflow,** weekly and by hand, outside project status: every named fault
  against its catching tests, and stability repeats of P0 and P1.

### Fixed
* A line too long in the counts tests failed lint in CI. Local runs had shown only the rounded score,
  so pylint is now judged by its exit code.

## 037a73a (2026-10-26)

### Fixed
* **The gate passed with about 257 of 1,016 tests missing.** The single P2-P4 job hit its one-hour
  budget, its workers then crashed serializing a keyword enum while skipping the rest, and the gate
  measured only the 759 results that existed. Keywords in verdicts and stop decisions are now plain
  strings, and every job lists the tests it selected so the gate counts a test without a result as not
  passed (`JOB_RESULT_MISSING`).
* **Windows functional jobs failed with `GREENMAIL_NOT_READY`.** The Windows runner ended Greenmail
  with the step that started it; Greenmail is now started through WMI so it outlives the step. Windows
  P0 then passed.

### Added
* **Test validity, built and run** (AMB-64): 9 named faults (for example a sender that drops one email,
  a reader that returns one too many), selected with `--emi-fault`, and `python -m emi_harness.validity
  faults | stability`. All 9 were caught by all 47 catching tests. The runs found two harness bugs
  (a fault wrapper hid the test broker client from `EmiApi`; a keyword enum crashed xdist workers) and
  two test gaps (counts and batches compared sets, so an extra email passed; `sender-drops-email` missed
  batches), all fixed. Faults never touch the session's probe email.
* **P2, P3 and P4 as three jobs side by side after P1** (AMB-67), instead of one P2-P4 job that
  exceeded its budget. Their 85% pass rate is measured over the three together by the quality gate.

### Changed
* **The status workflow is `joint-ci-workflow`** (AMB-66), renamed from `main`, which said nothing on
  its own. A manual run has a tick box per functional job; lint and unit tests always run first.

## b31d2e3 (2026-10-26)

### Added
* Greenmail diagnostics in the CI start step (Java version, JAR size, process ID, and on failure the
  process state and the last log lines), because the Windows functional job failed twice with
  `GREENMAIL_NOT_READY` and the log did not say why.

## 0925d6f (2026-10-26)

### Changed
* **A P0 failure stops nothing** (AMB-65). It used to abort the P0 job and the chain, hiding every
  other result. It is still critical (`JOB_CRITICAL_FUNCTIONAL_FAILURE`, the P0 job and project status
  fail), but the jobs run on and only the failed test's dependents are skipped, in every job. The CI
  jobs of one chain share a run ID and pass their results on so later jobs know which parents failed.

## 7cab8a6 (2026-10-26)

### Fixed
* Found by the first CI run: a harness unit test passed on Windows only, because pytest's pattern
  matching ignores case there; `allure-pages` ran when the gate was skipped; the checkout and setup
  actions moved off Node 20.

## 8882b7f (2026-10-26)

### Added
* **CI workflows:** lint, unit tests on Windows and Ubuntu, a P0 → P1 → P2-P4 functional chain per
  operating system, a quality gate over both that builds one Allure report and publishes it to GitHub
  Pages, standalone unit jobs per tool, and `ci-debug-run`.
* **Test validity design** (AMB-64, design §13.4) and the keyword `CASE_NO_VERIFICATION` for a test
  that checks nothing.

## fa6a112 (2026-10-26)

### Added
* **The functional suite of 1,016 cases:** delivery basics, counts per kind and recipient field,
  count exceeded, recipient fields not matched, marker forms, mail arriving during the wait,
  isolation, and 864 batch cases. Every parametrized case has its own TestRail case ID.
* Address slots (`EmiRun.address(slot)`, `EmiRun.addresses(count)`) and a per-wait `settle_window`.

## d372cc3 (2026-10-26)

### Added
* **Per-wait recipient fields (`match_fields`)** from `EmiApi` through the test broker to the reader,
  so Cc and Bcc can be tested; Bcc is matched on `Delivered-To`, which the sender stamps as Gmail does.

## 9007122 (2026-10-26)

### Changed
* The `.claude` directory structure and the README.

## c33e854 (2026-10-26)

### Added
* **First version:** the tools (`emi_reader`, `emi_sender`, `emi_greenmail`, `emi_test_broker`), the
  central API `EmiApi`, the test harness (pytest plugin and CLI), 354 unit tests, functional tests
  C400001 to C400005 passing against Greenmail, pylint 10.00/10, and the Phase 0 register, design
  document, keyword catalog, CI job description, RTM, decision log and README.

## 7614363 (2026-10-26)

* Initial commit (repository created).

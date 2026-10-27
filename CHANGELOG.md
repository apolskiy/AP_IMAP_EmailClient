# Change Log

What changed in each commit, newest first. Each entry says what was added, changed or fixed and why,
and names the CI run that found a problem when one did. Register rows (AMB-xx) are in
[docs/design/phase0_email_client_ambiguities.md](docs/design/phase0_email_client_ambiguities.md); the
reasoning behind each decision is in [CLAUDE_LOG.md](CLAUDE_LOG.md).

## Unreleased

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

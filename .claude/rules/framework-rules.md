<!--
SPDX-FileCopyrightText: 2026 Aleksandr Polskiy
SPDX-License-Identifier: Apache-2.0
-->
# Framework Architecture & CI/CD Gates

## 1. CI Pipeline & Quality Gates
**Triggers**: every push (including the push a merge into `main` creates) that changes tests, the
harness, or the tools, or the files that control how they run (requirements, `pyproject.toml`,
`.pylintrc`, workflow files), and every pull request into `main` with such changes. Docs-only
changes do not trigger CI. **Every CI job can also be started manually** at any time.

**Regression run `ci-regression-run`** (determines project status; a manual run chooses which of the
P0, P1, P2, P3 and P4 jobs run, always after lint and unit tests):
1. **Gate 1 - Static Analysis**: `pylint --rcfile=.pylintrc` across all modules achieving **10.00/10**.
2. **Gate 2 - Unit Tests (mandatory)**: every unit-test folder (`unittests/emi_reader/`, `unittests/emi_sender/`,
   `unittests/emi_test_broker/`, `unittests/emi_greenmail/`, `unittests/emi_harness/`, `unittests/emi_api/`) on Windows and Ubuntu matrix jobs. All must pass before any functional job starts; there is no point
   running P0-P4 if the harness or the tools are broken.
3. **Gate 3 - Functional Tests (Priority Sub-Jobs)**: once Gate 2 passes, the functional jobs run as a
   **chain: P0 → P1 → P2, P3 and P4** (the last three side by side, each its own job). Each job runs after
   the previous one finishes; the chain stops only when a
   job **aborts** (failures without an abort do not stop it). A skip is **not** a pass in any of the rules below.
   * `P0`: **Critical** tests. **100% must pass.** Every P0 failure is a critical functional failure
     (`JOB_CRITICAL_FUNCTIONAL_FAILURE`) and fails the P0 job, but stops nothing: the P0 job finishes,
     P1 and P2-P4 still run, and only the failed test's dependents are skipped, in P0, P1 and P2-P4.
   * `P1`: **High** priority tests. **100% must pass**; any failure or skip fails the job. Failures are
     isolated (the job continues) unless it aborts on an infrastructure failure.
   * `P2`, `P3`, `P4`: Standard and Low priority tests. Together they must reach **at least 85%** pass,
     otherwise the quality gate fails and nothing else is measured. Since the share holds for the three
     together, each of the three jobs fails only when it aborts.
   * **Overall**: (P0 + P1 + P2-P4 passed) / all functional tests must be **at least 95%**, otherwise the job fails.
4. **Gate 4 - CI Debug Job (`ci-debug-run`)**: A separate, non-blocking pipeline job supporting custom runs from explicit test lists or file inputs.

**Standalone unit jobs** (do **not** affect project status): one job per tool, so bugs can be ironed
out without waiting for `ci-regression-run`. Each runs automatically only when its own tool folder or
its own unit-test folder changes, and, like every CI job, can also be started manually. Failures are
reported but never turn the project status red.
* `unit-emi-reader`: `emi_reader/` or `unittests/emi_reader/`
* `unit-emi-sender`: `emi_sender/` or `unittests/emi_sender/`
* `unit-emi-test-broker`: `emi_test_broker/` or `unittests/emi_test_broker/`
* `unit-emi-greenmail`: `emi_greenmail/` or `unittests/emi_greenmail/`
* `unit-emi-harness`: `emi_harness/` or `unittests/emi_harness/`

**Overlapping runs** (when a new run starts while one is in progress):
* Pull requests and feature-branch pushes: the older run for the **same** PR/branch is **cancelled**.
* `main` (after merge): **never cancelled**; runs go in parallel (each job has its own VM and Greenmail),
  so every merge gets its own result.
* Manual runs, including `ci-debug-run`: never cancelled automatically; parallel allowed.
* Exception: a job using a **shared real mailbox** (e.g. Gmail) queues instead of overlapping, to
  respect the connection limit.
5. **CLI Capabilities**: Local command-line interface supports single test runs, file-based batch runs (one test name per line), and keyword-based execution (executing all tests tied to matching primary or secondary failure keywords).
6. **Dependency Skipping**: If a main/parent test fails, dependent tests are automatically skipped in the same or lower-priority job.
   A test depends only on tests of the same or a higher priority; a lower-priority parent fails collection
   (`HARNESS_DEPENDENCY_ORDER`). P2, P3 and P4 run side by side, so a P3 or P4 test whose P2 parent failed
   runs (and fails) instead of being skipped: a few extra failures cost less than waiting for P2.
7. **Reporting**: JUnit XML and HTML reports generated in parallel. Allure results published to GitHub Pages post-run (optional on CLI via flag).
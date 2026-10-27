<!--
SPDX-FileCopyrightText: 2026 Aleksandr Polskiy
SPDX-License-Identifier: Apache-2.0
-->
# CI Jobs: What Runs, When, and How

> Workflows: `.github/workflows/` (`ci-regression-run.yml`, `functional-chain.yml`, `unit-standalone.yml`,
> `unit-tool.yml`, `ci-debug-run.yml`, `test-validity.yml`). Source of decisions: `docs/design/phase0_email_client_ambiguities.md`
> (AMB-15, AMB-26, AMB-44, AMB-48, AMB-55, AMB-56, AMB-57, AMB-64, AMB-66).

## When CI runs

* On every **push** that changes tests, the harness, or the tools: `tests/`, `unittests/`, every
  `emi_*/` folder, `conftest.py`, and the files that control how they run (`requirements*.txt`,
  `pyproject.toml`, `.pylintrc`, `.github/`).
* **After merge**: merging into `main` creates a push to `main`, which triggers the same pipeline.
* **Documentation never triggers CI.** The trigger lists name only the paths above, so root Markdown
  (README, CHANGELOG, CLAUDE_LOG, CLAUDE.md), `docs/` and `.claude/` are left out, and a final `!**/*.md`
  drops Markdown even inside a listed folder. A push that mixes documentation and code still runs.
* One push can start two workflows: `ci-regression-run` and, when a tool folder or its unit tests
  changed, `unit-standalone` for that tool.
* On every **pull request into `main`** with such changes, so a change is tested merged with the
  current `main` before it lands.
* **By hand, at any time**: every CI job and pipeline can be started manually from GitHub
  (*Actions → select the workflow → Run workflow*), for example to re-run `ci-regression-run`
  without pushing. A manual `ci-regression-run` run has a tick box for each functional job (P0, P1,
  P2, P3, P4, all ticked by default): `lint` and `unit` always run first, then only the ticked jobs.

## When a new run starts while another is still running

| Trigger | What happens to the run already in progress | Why |
|---|---|---|
| Pull request / feature-branch push | **Cancelled** (same PR or branch only) | Its commit is already outdated; cancelling gives faster feedback on the new code. |
| `main` (after merge) | **Never cancelled**; both run in **parallel** | Every merge gets its own result, so a red `main` points at the merge that broke it. Each job has its own VM and Greenmail, so parallel runs cannot interfere. |
| Manual run, including `ci-debug-run` | **Never cancelled automatically**; parallel allowed | Someone started it on purpose. |
| Any job using a shared real mailbox (e.g. Gmail) | **Queued**, never overlapping | Gmail allows only about 15 connections per account. |

Queuing is deliberately not used on `main`: GitHub keeps only one waiting run per group and drops
older waiting runs, so a quick series of merges could leave some merges untested.

## Regression run `ci-regression-run` (decides project status)

The workflow joins every gate into one run (named `joint-ci-workflow` until 2026-10-27; GitHub lists its
earlier runs under that name). The jobs run in this order. `lint` and `unit` must **pass** before anything after them starts. The
functional jobs then run as a chain, **P0 → P1 → P2, P3 and P4** (the last three side by side, each on
its own runner, AMB-67): each starts when the previous one has finished, and the chain stops only if a
job **aborts** (an infrastructure failure, the circuit breaker, or the job's time budget). A job that
finishes with failures, P0 failures included, lets the next one run. Only the dependents of a failed
test are skipped, in its own job and in the later ones: the jobs share one run ID, and each later job
first gets the P0 and P1 results.

| # | Job | Runs on | Blocking | What it does |
|---|---|---|---|---|
| 1 | `lint` | Ubuntu | **Yes** | `pylint --rcfile=.pylintrc` across all modules; must score **10.00/10**. |
| 2 | `unit` | Windows + Ubuntu | **Yes** (mandatory gate) | Every unit-test folder: `unittests/emi_reader/`, `unittests/emi_sender/`, `unittests/emi_test_broker/`, `unittests/emi_greenmail/`, `unittests/emi_harness/`, `unittests/emi_api/`. All must pass; otherwise no functional job starts. |
| 3 | `functional-p0` | Windows + Ubuntu (see below) | **Yes** | Starts Greenmail and `emi_test_broker`, then runs the P0 (critical) tests. **100%** must pass; a skip is not a pass. **Any P0 failure is a critical failure** (`JOB_CRITICAL_FUNCTIONAL_FAILURE`): the job fails but finishes its other tests, P1 and P2–P4 still run, and only the failed test's dependents are skipped. |
| 4 | `functional-p1` | same as P0 | **Yes** | P1 (high-priority) tests, after P0. **100%** must pass; failures are isolated unless the job aborts. |
| 5 | `functional-p2`, `functional-p3`, `functional-p4` | same as P0 | **Yes** (fail only on an abort) | The P2, P3 and P4 tests, three jobs side by side after P1. Together **≥ 85%** must pass; since that holds for the three together, `quality-gate` measures it, and each job fails only when it aborts. Each job gets the P0 and P1 results first; the three do not see each other's, so a P3 or P4 test whose P2 parent failed runs and fails instead of being skipped (accepted for the speed). |
| 6 | `quality-gate` | Ubuntu | **Yes** | Reads all functional results. Fails if P0 or P1 < 100%, or P2–P4 < 85% (then nothing else is measured), or overall passed / all functional < **95%**. Prints a keyword summary table and the effective P2–P4 floor. |

**Project status is green only if every job above is green.**

**Manual run with chosen jobs (AMB-66):** a job left out neither runs nor stops the jobs after it; a
chosen job still waits for the chosen jobs before it and does not run if one of them aborted. Its tests'
parents in a left-out job have no result, so they are not skipped for it. `quality-gate` measures the
results that exist (a left-out priority counts as 0 of 0); with no functional job ticked it does not run.

**Functional jobs on Windows and Ubuntu:** each operating system runs its own P0 → P1 → P2–P4 chain,
side by side; both must pass. That is 2 × 5 functional jobs, each on its own runner with its own Greenmail. Ubuntu runs Greenmail as a Docker container. GitHub's Windows machines
cannot run that image, so there Greenmail runs as a Java program (JAR, Java 17, preinstalled on the
runners), started through WMI so that it outlives the step that starts it. Windows is tested for real because the broker's background process, port handling, log-file
sharing and socket errors behave differently there. The Allure report is built once, on Ubuntu, from
both systems' results.

Before the first test, each functional job sends a **probe email** of each type and checks that it
arrives in a folder the reader can open. If not, the job stops at once and every test is skipped
(`HARNESS_PROBE_EMAIL_NOT_RECEIVED`).

## Jobs that do not affect project status

| Job | When | What it is for |
|---|---|---|
| `ci-debug-run` | On demand (GitHub *Actions → Run workflow*), with a test list, a test-list file, or a keyword | Custom ad-hoc runs while developing or debugging tests. Its report includes the `emi_test_broker` log for the run's time window and all port-open failures of that day. |
| `unit-emi-reader` | Automatically when `emi_reader/` or `unittests/emi_reader/` changes; also by hand | Reader library unit tests on their own, to iron out bugs. Failures are reported, never block. |
| `unit-emi-sender` | Automatically when `emi_sender/` or `unittests/emi_sender/` changes; also by hand | Sender unit tests on their own. Never block. |
| `unit-emi-test-broker` | Automatically when `emi_test_broker/` or `unittests/emi_test_broker/` changes; also by hand | Test broker unit tests on their own. Never block. |
| `unit-emi-greenmail` | Automatically when `emi_greenmail/` or `unittests/emi_greenmail/` changes; also by hand | Greenmail client unit tests on their own. Never block. |
| `unit-emi-harness` | Automatically when `emi_harness/` or `unittests/emi_harness/` changes; also by hand | Harness unit tests on their own. Never block. |
| `test-validity` | Weekly (Monday 03:00 UTC: every named fault, and P0 and P1 repeated 3 times); by hand with a choice of check, fault, job to repeat and repeats | Proves the functional tests fail when a tool is broken (named faults) and pass when it works (repeat runs, timing margins); design §13.4. Ubuntu. Never blocks. |

The standalone unit jobs run on Windows and Ubuntu. A manual run of `unit-standalone` offers one tool or
all of them.

**Counts on the run page** (AMB-72): every functional job ends its log with one line, e.g.
`P2 job: 406 selected, 400 passed, 4 failed, 2 skipped, 0 without a result`, and writes the same counts
to the run's summary page under a title such as `P2 job (Linux)`, followed by up to 50 tests that did not
pass with their keyword, step and message. `quality-gate` adds its per-priority report there too.

**Allure on GitHub Pages:** after a run on `main`, the `quality-gate` job builds one Allure report from
both operating systems' results, and `allure-pages` publishes it. Turn this on once under *Settings →
Pages → Source: GitHub Actions*. Until then `allure-pages` fails without affecting project status, and
the report is still available as the `allure-report` artifact of every run.

**Greenmail password:** optional repository secret `EMI_GREENMAIL_PASSWORD`. Without it the workflows
use Greenmail's public test password `password`.

## Running the same jobs locally (CLI)

Run these from the repository root after the Greenmail setup in the README. They mirror the CI jobs.

| CI job | Local command |
|---|---|
| `lint` | `pylint --rcfile=.pylintrc emi_reader emi_sender emi_test_broker emi_greenmail emi_api emi_harness tests unittests conftest.py` |
| `unit` | `python -m pytest unittests` |
| `unit-emi-reader` | `python -m pytest unittests/emi_reader` |
| `unit-emi-sender` | `python -m pytest unittests/emi_sender` |
| `unit-emi-test-broker` | `python -m pytest unittests/emi_test_broker` |
| `unit-emi-greenmail` | `python -m pytest unittests/emi_greenmail` |
| `unit-emi-harness` | `python -m pytest unittests/emi_harness` |
| `functional-p0` … `functional-p4` | `python -m emi_harness.cli --priority P0` (or `P1`, `P2`, `P3`, `P4`; locally also `P2-P4` as one job, which itself needs 85%) |
| `quality-gate` | `python -m emi_harness.gate <ubuntu reports> <windows reports> --expect P0 ... --expect P4` (one `--expect` per chosen job) |
| `ci-debug-run` | `python -m emi_harness.cli --test <name>` / `--file <list.txt>` / `--keyword <KEYWORD>` |
| `test-validity` | `python -m emi_harness.validity faults [--fault <name>]`; `python -m emi_harness.validity stability --priority P1 --repeat 3` |

`emi_test_broker` starts automatically on the first test that needs it (port 8025 by default).

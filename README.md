# AP_IMAP_EmailClient

An email tool, a Python library and a test harness for end-to-end tests that wait for emails sent by
an application (account activation, site invitation, …) and extract the link behind the email's main
button, with many tests running in parallel against one shared mailbox.

The **tool** sends emails over SMTP (playing the application) and checks for them over IMAP. It runs
as a background process, the test broker (port 8025), which every test run on the machine shares, along
with its mail-server connections and its log. The tool also has command lines of its own
(`python -m emi_sender`, `python -m emi_test_broker`). The **library** is the Python API tests call
(`EmiApi`). It hands each send and wait to the tool rather than talking to the mail server itself. The
**harness** runs the tests, applies priorities and gates, and writes the reports.

> **Status:** implementation in progress. The reader, sender, Greenmail client, test broker, central
> API (`EmiApi`) and test harness are built, unit-tested and checked against Greenmail, and the
> functional suite has 1,019 test cases, with CI workflows and test-validity checks (design §13.4).
> What is built and what is not yet is tracked in [docs/rtm.md](docs/rtm.md).

## Getting Started

### Prerequisites

| Tool | Version | Used for | Required |
|---|---|---|---|
| Git | any recent | getting the code | yes (or use the zip archive) |
| Python | 3.14 or later | the library, tools and tests | yes |
| Docker | Docker Desktop with WSL 2 (Windows), Docker Desktop (macOS) or Docker Engine (Linux) | running Greenmail locally | yes, unless Greenmail runs as a Java program |
| Java | 11 or later | Greenmail as a standalone JAR without Docker; the Allure command line | optional |
| Allure command line | 2.x | turning Allure results into an HTML report | optional |

Check what is installed: `git --version`, `python --version`, `docker version`, `java -version`,
`allure --version`.

**Installing Docker.** Windows and macOS: install Docker Desktop from
<https://www.docker.com/products/docker-desktop/> (on Windows, accept the WSL 2 option), then start
Docker Desktop and wait until it reports that the engine is running. Linux (Ubuntu): follow
<https://docs.docker.com/engine/install/ubuntu/>, then `sudo usermod -aG docker $USER` and log in
again so `docker` works without `sudo`. Check with `docker run --rm hello-world`.

### 1. Get the code

From the public repository:

```bash
git clone https://github.com/apolskiy/AP_IMAP_EmailClient.git
cd AP_IMAP_EmailClient
```

To update an existing copy later: `git pull`. If you received a zip archive instead, unpack it and
open a terminal in the unpacked folder.

### 2. Create a Python environment and install dependencies

Bash (Linux/macOS/Git Bash):

```bash
python -m venv .venv
source .venv/bin/activate            # Git Bash on Windows: source .venv/Scripts/activate
pip install -r requirements.txt -r requirements-dev.txt
```

PowerShell (Windows):

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt -r requirements-dev.txt
```

### 3. Set up and start Greenmail on your machine

[Greenmail](https://greenmail-mail-test.github.io/greenmail/) is a test mail server (SMTP + IMAP)
that runs in Docker. The tests use it locally and in CI; the tool itself works with any SMTP and IMAP server.

**a) Get the image** (once; Docker Desktop must be running). The project uses the official
`greenmail/standalone` image from Docker Hub, <https://hub.docker.com/r/greenmail/standalone>, at tag
`2.1.15`; that page lists the other tags and the `GREENMAIL_OPTS` settings.

```bash
docker pull greenmail/standalone:2.1.15
```

**b) Create and start the container** (once). `docker run` creates a container named
`greenmail_server` from the image and starts it in the background.

Bash:

```bash
docker run -d \
  --name greenmail_server \
  -p 3025:3025 \
  -p 3143:3143 \
  -p 3993:3993 \
  -p 8080:8080 \
  -e GREENMAIL_OPTS="-Dgreenmail.setup.test.all -Dgreenmail.hostname=0.0.0.0 -Dgreenmail.users=qa:password@example.com" \
  greenmail/standalone:2.1.15
```

PowerShell:

```powershell
docker run -d `
  --name greenmail_server `
  -p 3025:3025 `
  -p 3143:3143 `
  -p 3993:3993 `
  -p 8080:8080 `
  -e GREENMAIL_OPTS="-Dgreenmail.setup.test.all -Dgreenmail.hostname=0.0.0.0 -Dgreenmail.users=qa:password@example.com" `
  greenmail/standalone:2.1.15
```

This creates the shared mailbox user `qa@example.com` (login `qa`, password `password`).

| Port | Service | Used by |
|---|---|---|
| 3025 | SMTP | the sender tool |
| 3143 | IMAP (plain) | the tool's IMAP reader (default) |
| 3993 | IMAPS (TLS) | TLS tests |
| 8080 | Greenmail REST API | readiness check |

**c) Check that it is ready:**

```bash
docker ps --filter name=greenmail_server                  # STATUS should say "Up"
curl http://127.0.0.1:8080/api/service/readiness          # Bash
```
```powershell
Invoke-RestMethod http://127.0.0.1:8080/api/service/readiness   # PowerShell
```

Expected answer: `{"message":"Service running"}`. If not, see `docker logs greenmail_server`.

**d) Later sessions: start the existing container** (after a reboot or `docker stop`). Do not run
`docker run` again; the name is already taken.

```bash
docker start greenmail_server     # start the container created in b)
docker stop greenmail_server      # stop it
docker rm -f greenmail_server     # remove it (then repeat b) to create it again)
```

Greenmail keeps mail in memory, so restarting the container empties the mailbox. `docker run`
fails with "name already in use" if the container exists: use `docker start` instead, or remove it first.

**Windows without Docker: Greenmail as a Java program.** Install a Java runtime 11 or later (for
example Eclipse Temurin from <https://adoptium.net>), check it with `java -version`, then in PowerShell:

```powershell
Invoke-WebRequest https://repo1.maven.org/maven2/com/icegreen/greenmail-standalone/2.1.15/greenmail-standalone-2.1.15.jar -OutFile greenmail-standalone-2.1.15.jar
java "-Dgreenmail.setup.test.all" "-Dgreenmail.hostname=127.0.0.1" "-Dgreenmail.users=qa:password@example.com" -jar greenmail-standalone-2.1.15.jar
```

It uses the same ports and user as the Docker container and runs in the foreground; stop it with
Ctrl+C. Keep the JAR outside the repository folder. The readiness check above works the same way.

**Different ports or another host:** publish other host ports (for example `-p 13143:3143`) and
pass them to the harness, e.g. `--emi-imap-host`, `--emi-imap-port`, `--emi-smtp-port`, or the matching
`EMI_*` environment variables. See [Using another mail server](#using-another-mail-server-and-credentials).

**How all test emails land in one inbox:** tests use addresses like
`qa+<TestRail case ID>_<run id>@example.com` (e.g. `qa+C400001_20261026t153012-w03-a1b2@example.com`). Gmail delivers all `qa+…` addresses to the `qa` mailbox;
Greenmail does not. On Greenmail, our sender tool therefore delivers every email to the base mailbox
`qa@example.com` (SMTP envelope recipient) while the `To:` header keeps the unique `qa+…` address.
No Greenmail configuration is needed beyond the `qa` user above.

### 4. Run the tests from the command line

Run every command from the repository root with the virtual environment from step 2 active
(`source .venv/bin/activate`, or `.\.venv\Scripts\Activate.ps1` in PowerShell). The unit tests need no
server; the functional tests need Greenmail from step 3 running.

```bash
pylint --rcfile=.pylintrc emi_reader emi_sender emi_test_broker emi_greenmail emi_api emi_harness tests unittests conftest.py
python -m pytest unittests                       # all unit tests
python -m emi_harness.cli                        # every functional test (job "all")
python -m emi_harness.cli --priority P0         # one job: P0, P1, P2, P3, P4, P2-P4, debug or all
python -m emi_harness.cli --test C400001        # one test: case ID, function name or node ID; repeatable
python -m emi_harness.cli --file smoke.txt      # one test name per line
python -m emi_harness.cli --keyword READER_     # tests tied to a keyword or a keyword prefix
python -m emi_harness.cli -n 4                  # 4 parallel workers (capped at --emi-max-connections)
python -m emi_harness.cli --allure --calibrate  # also Allure results; record durations, suggest limits
python -m emi_harness.gate reports/<run>        # quality gates over a run's results
```

Every `--emi-*` setting (`python -m emi_harness.cli --help`) can also be given as an `EMI_*`
environment variable or in `.env`; a command-line value wins. Plain `python -m pytest tests` runs the
same functional tests with the same `--emi-*` options.

The functional suite (`tests/functional/`) has 1,019 test cases. The 5 critical (P0) ones send one email of
each kind (activation and invitation must yield their links, generic must yield none), and, per link kind,
wait for that link while two other emails arrive first. The others: delivery basics, counts per kind and
recipient field, recipient fields not matched, markers, mail arriving during the wait, isolation from
other tests' mail, and 864 batch cases (every distribution preset × send order × recipient field × kind
mix × concurrency × seed). Each parametrized case is its own TestRail case, so `--test C410123` runs
exactly one. The ID blocks are listed in `tests/functional/catalog.py`. Run one worker at a time and the
whole suite takes about an hour against a local Greenmail; `-n 4` shortens it. In CI, with 4 workers per
job and P2, P3 and P4 side by side, the first full run passed all 1,016 tests then in the suite on both
operating systems in about 20 minutes.

Reports go to `reports/<run>/<job>/`: `junit.xml`, `report.html`, `results-<worker>.jsonl` (one line
per test with its keywords and step log), `settings.json` (every setting with its source, passwords
hidden), `attachments/<case id>/` (per-test evidence: sender manifests, a failing tool's attempt
history and folder accessibility matrix, anything a test attaches) and, with `--allure`,
`allure-results/` (HTML too when the Allure CLI is installed, see step 5). Exit codes: `0` the job passed, `1` it failed, `2` it stopped early or an
earlier job of the run aborted, `4` invalid options, `5` no tests selected.

The shared EMI test broker (port 8025) starts automatically, in the background without a window, with
the first test that needs it. Before the first test the harness checks that Greenmail and the broker
are reachable and sends a probe email of each kind (case `C000000`); if either fails, every test is
skipped with the keyword naming the cause, and the message says which host and port were checked.

**Firewall: allow port 8025.** If a firewall runs on this machine (Windows Defender Firewall with
custom rules, a corporate endpoint-security product, `ufw` or `firewalld` on Linux), TCP port **8025**
must be open for local connections. Otherwise the tests cannot reach the test broker, and every test is
skipped with `TEST_BROKER_UNAVAILABLE` (or `TEST_BROKER_PORT_UNAVAILABLE`). The broker listens on
`127.0.0.1` only, so the port only needs to be open on this machine, not to the network. If you move
the broker with `EMI_TEST_BROKER_PORT`, open that port instead. On Windows, a firewall prompt for
`python.exe` may appear the first time the broker starts; allow it for private networks.

```powershell
# Windows, PowerShell as Administrator
New-NetFirewallRule -DisplayName "EMI test broker" -Direction Inbound -Protocol TCP -LocalPort 8025 -RemoteAddress 127.0.0.1 -Action Allow
```
```bash
sudo ufw allow from 127.0.0.1 to any port 8025 proto tcp                                     # Ubuntu, ufw
sudo firewall-cmd --permanent --zone=trusted --add-source=127.0.0.1 && sudo firewall-cmd --reload  # firewalld
```

Greenmail's ports (3025, 3143, 3993, 8080) are also reached on this machine and need the same treatment
if the firewall filters local traffic.

### 5. Optional: Allure reports (Java and the Allure command line)

JUnit XML and the HTML report are always written and need nothing extra. Allure is optional. The
`allure-pytest` package from step 2 writes Allure *results*, and turning them into an HTML report needs
the Allure command line, which runs on Java.

1. **Java 11 or later.** Windows: `winget install EclipseAdoptium.Temurin.21.JRE` (or the installer from
   <https://adoptium.net>). macOS: `brew install --cask temurin`. Ubuntu: `sudo apt install openjdk-21-jre`.
   Open a new terminal and check `java -version`.
2. **Allure command line 2.x.** Windows: `scoop install allure` (with [Scoop](https://scoop.sh)), or
   download the zip from <https://github.com/allure-framework/allure2/releases>, unpack it and add its
   `bin` folder to `PATH`. macOS: `brew install allure`. Ubuntu: the `.deb` from the same releases page
   (`sudo apt install ./allure_<version>-1_all.deb`). Check `allure --version`.
3. **Run with Allure:**

   ```bash
   python -m emi_harness.cli --priority P0 --allure
   allure open reports/<run>/P0/allure-report       # opens the generated HTML in a browser
   ```

   Without the Allure command line, the run still passes or fails as usual, keeps the raw results in
   `allure-results/`, and prints a warning that no Allure HTML was built.

**What the Allure report shows.** CI publishes one report from both operating systems to GitHub Pages
after each run on `main`.

* **Suites** groups every test by priority, P0 to P4. **Graphs** shows the same split as severity:
  blocker (P0), critical (P1), normal (P2), minor (P3), trivial (P4).
* **Categories** groups the tests that did not pass by cause: Greenmail, test broker, reader, sender,
  public API, harness, job stopped, skipped because a parent test did not pass, email not received, more
  emails than expected, link problems, verification failed, other test-case failures. Each message starts
  with its keyword, e.g. `[CASE_EMAIL_NOT_RECEIVED] step S02 ...`. Allure lists only failed, broken and
  skipped tests here, so the tab is empty when everything passed.
* A test that ran on both Windows and Ubuntu appears once, with the other run under **Retries**.

### Using another mail server, and credentials

The project targets **Greenmail** and needs no credentials beyond the local `qa` user above. To run
against another IMAP/SMTP server (for example Gmail with an App Password), set these parameters.
**Credentials are never written to logs, reports or the `/health` output, and never committed.**

| Parameter | Meaning |
|---|---|
| `EMI_IMAP_HOST`, `EMI_IMAP_PORT` | IMAP server the reader connects to |
| `EMI_IMAP_SECURITY` | `plain`, `ssl` or `starttls` |
| `EMI_IMAP_USER`, `EMI_IMAP_PASSWORD` | IMAP login of the shared mailbox |
| `EMI_SMTP_HOST`, `EMI_SMTP_PORT` | SMTP server the sender uses |
| `EMI_SMTP_SECURITY` | `plain`, `ssl` or `starttls` |
| `EMI_SMTP_USER`, `EMI_SMTP_PASSWORD` | SMTP login, if the server requires one |

* **CLI:** copy `.env.example` to `.env` in the repository root and fill in the values. `.env` is
  git-ignored; `.env.example` contains the parameter names only.
* **CI:** add the same names as repository secrets (*Settings → Secrets and variables → Actions*);
  the workflows pass them to the jobs as environment variables.

**Your own Greenmail password (optional).** By default the tools log in as Greenmail's local test
user `qa` with the public password `password`, so nothing needs configuring. To use your own:

1. Start Greenmail with it: replace `qa:password@example.com` with `qa:<your password>@example.com`
   in the `docker run` (or `java`) command above.
2. Locally, put `EMI_IMAP_PASSWORD=<your password>` and `EMI_SMTP_PASSWORD=<your password>` in `.env`.
3. In CI, add a repository secret `EMI_GREENMAIL_PASSWORD`; the workflows start Greenmail with it and
   pass it to the tests as `EMI_IMAP_PASSWORD` / `EMI_SMTP_PASSWORD`.
* Real servers limit simultaneous connections; see [IMAP Connection Limit](#imap-connection-limit).

## Test Priorities and Failing Fast

* **P0 and P1** are must-pass tests that prove core functionality. **P0 tests are the critical
  tests; P1 tests are the high-priority tests.** Together they may be at most **20% of all tests**
  and use at most **20% of the run time**.
* **What each priority holds today** (1,019 tests):

  | Priority | Tests | What |
  |---|---|---|
  | P0 | 5 | one email of each kind: activation and invitation yield their links, generic yields none; per link kind, a wait that skips two other emails for the later matching one |
  | P1 | 9 | one email of each kind in To, Cc and Bcc, counted exactly |
  | P2 | 406 | every batch preset × order × field × kind mix × concurrency with seeds 1-2; counts of 2 to 10; fields, markers, arrival during the wait |
  | P3 | 311 | the same batches with seeds 3-4; counts exceeded, isolation, other delivery checks |
  | P4 | 288 | the same batches with seeds 5-6 |

  Batches are split by seed rather than by preset so the three side-by-side jobs send about the same
  mail and take about as long. Each job's Greenmail mailbox fills as it runs, and a fuller mailbox makes
  every wait slower.
* **Any P0 failure** is a critical failure (`JOB_CRITICAL_FUNCTIONAL_FAILURE`): the P0 job fails and
  so does the project status, but nothing stops. The P0 job finishes its other tests, P1 and P2–P4
  still run, and only the tests that depend on the failed one are skipped, in every job.
* **Too many P0/P1 tests:** the harness automatically moves the excess **P1** tests to **P2** for
  that run, logs each move (`JOB_PRIORITY_DOWNGRADED`) and suggests a permanent re-label. P0 tests are
  never moved; if P0 alone exceeds 20%, the run stops with `JOB_PRIORITY_P0_OVERFLOW`.
* **Skips:** any test not executed because of an environment problem or an aborted job is reported
  as a skip with the keyword that caused it.
* **Stopping early:** an environment failure (server or service unreachable, network fault that
  retries did not fix) stops any job. In P2–P4 and debug runs, 5 failures in a row with the same
  keyword stop the job (`JOB_CIRCUIT_BREAKER_OPEN`). Other test failures never stop a job.
* **Order:** if pylint or the unit tests fail, nothing else runs. Functional jobs run P0 → P1 →
  P2, P3 and P4 side by side; if the P0 or P1 job aborts, the lower-priority jobs do not run.
* **Inside a test:** a failing step fails the test, the remaining steps are skipped, and cleanup always runs.

## CI Jobs and How to Run Them

CI runs on every push and every pull request into `main` that changes tests, the harness or the
tools, including the push a merge into `main` creates. Every CI job can also be started by hand
from GitHub (*Actions → Run workflow*). Full details, with local commands for every job: [docs/ci_jobs.md](docs/ci_jobs.md).

**Regression run `ci-regression-run` (decides project status), in order:**

1. `lint`: pylint must score 10.00/10.
2. `unit`: the unit tests of every tool, the harness and the central API, on Windows and Ubuntu.
   If any fail, no functional tests run.
3. `functional-p0` → `functional-p1` → `functional-p2`, `functional-p3` and `functional-p4` (the last
   three side by side): each starts Greenmail and the EMI test broker, then runs its tests. If a job
   aborts (environment failure, circuit breaker, time budget), the ones after it do not run; test
   failures alone never stop the chain. The P2, P3 and P4 jobs fail only when they abort: their 85% is
   measured over the three together, by `quality-gate`.
4. `quality-gate`: P0 and P1 must pass 100% (a skip is not a pass), P2–P4 together at least 85%,
   and all functional tests together at least 95%.

Started by hand, `ci-regression-run` offers a tick box per functional job (P0, P1, P2, P3, P4); `lint` and
`unit` always run first, then only the ticked jobs.

**Operating systems:** unit and functional tests run on both **Windows and Ubuntu**, side by side,
and both must pass. Ubuntu runs Greenmail in Docker; GitHub's Windows machines cannot run that image,
so there Greenmail runs as a Java program (the same JAR as in the Windows setup above, on the Java the
runners already provide, started through WMI so that it outlives the step that starts it). Windows gets real functional runs because the test broker's background
process, port handling, log-file sharing and socket errors behave differently there.

**Probe email:** before the first test, each functional run sends a probe email of each type and
checks it arrives in a folder the reader can open. If it does not, the run stops at once and all
tests are skipped (`HARNESS_PROBE_EMAIL_NOT_RECEIVED`).

**Not part of project status:** `ci-debug-run` (run by hand for chosen tests, a test-list file or a
keyword), one standalone unit job per tool (`unit-emi-reader`, `unit-emi-sender`,
`unit-emi-test-broker`, `unit-emi-greenmail`, `unit-emi-harness`) for fixing bugs, each run only when its own tool or its own unit
tests change, and `test-validity` (weekly and by hand, see below).

**Test validity:** a passing test is only worth something if it would fail when the tool is broken.
`test-validity` runs each functional test that should catch a deliberately broken tool (a **named
fault**, such as a sender that drops one email or a reader that returns one email too many) with that
fault switched on, and checks it fails with the expected keyword. It also repeats tests on a healthy
setup to find flaky tests and time limits that are too tight. A test that would pass while checking
nothing fails with `CASE_NO_VERIFICATION` in every run. Results produced under a fault are never
accepted by the quality gate.

**Overlapping runs:** a new run on a pull request or feature branch cancels the older one for that
branch. Runs on `main` and manual runs are never cancelled and can run side by side.

**Locally:**

```bash
python -m pytest unittests                       # unit gate
python -m emi_harness.cli --priority P0         # one job (P0, P1, P2, P3, P4, or P2-P4 as one)
python -m emi_harness.cli --test <name>         # one test; also --file <list.txt>, --keyword <KEYWORD>
python -m emi_harness.validity faults           # every named fault against its catching tests
python -m emi_harness.validity stability --priority P1 --repeat 3   # flaky tests, thin time margins
```

## Documentation Map

Where each design topic is written down. The design describes the whole intended system; the RTM
says how much of it is built.

| File | What it contains |
|---|---|
| [docs/design/phase0_email_client_ambiguities.md](docs/design/phase0_email_client_ambiguities.md) | **Ambiguity register (Phase 0).** Every open question (AMB-01 to AMB-70) with its severity, the options considered, the recommendation and the author's decision; the status of open questions; **Spike Evidence**: measurements and bugs found against a live Greenmail that decisions rest on. |
| [docs/design/email_client_harness_design.md](docs/design/email_client_harness_design.md) | **Design document (Phase 2)**, each section citing the register rows it implements (see the next table). |
| [docs/rtm.md](docs/rtm.md) | **Requirements traceability matrix.** Each requirement → design section → code → unit test IDs → status (Done / Partial with the gap named / Planned). |
| [docs/keywords.md](docs/keywords.md) | **Failure keyword catalog.** Keyword grammar; every `READER_`, `SENDER_`, `TEST_BROKER_`, `GREENMAIL_`, `API_`, `HARNESS_`, `CASE_` and `JOB_` keyword with its meaning, outcome (fail / skip / skip + stop / info) and the first thing to check. A unit test fails if code uses a keyword missing here. |
| [docs/ci_jobs.md](docs/ci_jobs.md) | **CI jobs.** When CI runs, overlapping runs, the regression run `ci-regression-run` (lint, unit, P0 → P1 → P2 / P3 / P4, quality gate), the standalone jobs, and the local command for each job. |
| [CHANGELOG.md](CHANGELOG.md) | **Change log.** What changed in each commit, newest first, with the CI runs that found the problems it fixed. |
| [CLAUDE_LOG.md](CLAUDE_LOG.md) | **Decision and quality log.** What was built in each phase and step, key architectural decisions with their register rows, and pylint scores over time. |
| [CLAUDE.md](CLAUDE.md), [.claude/rules/](.claude/rules) | **Working rules** the AI collaborator followed: no AI commits, design before code, pylint 10.00/10, naming, test IDs, shell rules. |

Design document sections:

| Section | Topic | Code |
|---|---|---|
| §1 | Purpose and scope | n/a |
| §2 | Architecture: tools, dependencies between them, repository layout trade-off | all packages |
| §3 | Configuration: option table, precedence (argument > CLI > env > `.env` > default), secrets | `emi_harness/options.py`, each tool's `settings.py` |
| §4 | Identifiers: TestRail case IDs `C<number>`, run IDs, addresses, marker headers | `emi_api/identity.py`, `emi_sender/compose.py` |
| §5 | Reader: transport, connections, folder accessibility, waiting and matching, retries, MIME and CTA links, tokens, inventory | `emi_reader/` |
| §6 | Sender: templates, tokens, batches and distributions, delivery (SMTP / `APPEND`), validation, manifest and exit codes | `emi_sender/` |
| §7 | Test broker: shared local service, port rules, auto-start, run leases, dated log, purge | `emi_test_broker/` |
| §8 | Greenmail client (§8.1) and the central API `EmiApi` (§8.2): modes, `health()`, `EmiError`, families | `emi_greenmail/`, `emi_api/` |
| §9 | Harness: CLI (§9.1), step model (§9.2), outcomes and keywords (§9.3), failing fast (§9.4), dependencies (§9.5), session setup and probe email (§9.6), priorities and gates (§9.7), reports (§9.8) | `emi_harness/` |
| §10 | Time budgets: step, test, job, send, wait | `emi_harness/steps.py`, `plugin.py` |
| §11 | IMAP connection limit and the connection budget | `emi_test_broker/leases.py`, `emi_harness/cli.py` |
| §12 | CI: regression run, functional chain per OS, standalone and debug jobs, overlapping runs | `.github/workflows/`, `.github/actions/` |
| §13 | Test strategy: unit tests, network faults in three layers, functional tests, test validity (§13.4) | `unittests/`, `tests/`, `emi_harness/faults.py`, `validity.py` |
| §14 | Dependencies and project files | `requirements*.txt`, `pyproject.toml` |
| §15 | What was left out | see [What Was Left Out](#what-was-left-out) |

## Project Structure

```text
AP_IMAP_EmailClient/
├── CLAUDE.md                  # AI collaborator instructions: safeguards, structure, naming
├── CLAUDE_LOG.md              # Architectural decisions and pylint scores, by phase
├── .claude/
│   ├── README.md              # What the AI collaborator's configuration contains
│   └── rules/                 # Rules the AI collaborator followed throughout the project
│       ├── claude_md_shell_rules.md   # Shell use on Windows: no heredocs, scripts run by file
│       ├── code-style.md              # Naming, private state, SPDX header
│       ├── framework-rules.md         # CI gates, priorities P0-P4, standalone unit jobs
│       ├── skill-rules.md             # Lifecycle phases 0-4 and audit logging
│       ├── testing-standards.md       # Test naming, TestRail IDs, ASCII-only identifiers
│       └── worktree-rules.md          # No AI commits, secrets protection, IDE ignores
├── .github/
│   ├── workflows/             # CI: regression run, functional chain per OS, standalone unit jobs, debug run
│   └── actions/               # Shared CI steps: start Greenmail, run one harness job
├── docs/
│   ├── ci_jobs.md             # CI jobs and how to run them
│   ├── keywords.md            # Failure keyword catalog
│   ├── rtm.md                 # Requirements traceability: design → code → tests → status
│   └── design/
│       ├── phase0_email_client_ambiguities.md   # Ambiguity register and spike evidence
│       └── email_client_harness_design.md       # Phase 2 design document
├── emi_reader/                # IMAP reader: wait for a test's email, extract the CTA link
├── emi_sender/                # Sender: realistic emails from the samples, batches, SMTP / APPEND
│   └── templates/             # JSON templates (activation, invitation, generic)
├── emi_greenmail/             # Greenmail client: readiness, users, purge (REST API)
├── emi_test_broker/           # Shared local broker: connections, run leases, dated log, purge
├── emi_api/                   # Central API (EmiApi): the single entry point tests call
├── emi_harness/               # Test harness: pytest plugin, CLI, steps, job rules, gates, reports
├── tests/
│   ├── functional/            # Functional tests against Greenmail (TestRail IDs 400000-599999)
│   └── fixtures/eml/          # The sample emails the sender's templates are built on
├── unittests/                 # One folder per tool, each with its own CI job
│   ├── emi_reader/
│   ├── emi_sender/
│   ├── emi_greenmail/
│   ├── emi_test_broker/
│   ├── emi_api/
│   └── emi_harness/
├── reports/                   # Run reports are written here (contents never committed)
├── conftest.py                # Loads the harness pytest plugin
├── .env.example               # Setting names only, never values
├── .pylintrc                  # Pylint rules (10.00/10 gate)
├── pyproject.toml             # Python 3.14, pytest discovery of EMI_* tests
├── requirements.txt           # Runtime dependencies (pinned)
└── requirements-dev.txt       # Test and lint dependencies (pinned)
```

## Design Decisions

Full design: [docs/design/email_client_harness_design.md](docs/design/email_client_harness_design.md).
Every decision, with the options considered: [docs/design/phase0_email_client_ambiguities.md](docs/design/phase0_email_client_ambiguities.md).
Failure keywords: [docs/keywords.md](docs/keywords.md).
What is built and tested so far, requirement by requirement: [docs/rtm.md](docs/rtm.md).

### Key decisions

* **One shared inbox, unique addresses.** Each test uses `qa+<test id>_<run id>@example.com`, so tests
  and parallel runs never pick up each other's emails, and mail from earlier runs is ignored.
* **TestRail case IDs in every email.** The test ID in the address, the `X-EMI-Test-Id` header and the
  body marker line is the TestRail case ID, e.g. `qa+C400001_20261026t153012-w03-a1b2@example.com` and
  `X-EMI-Test-Id: C400001`. Any email can be matched to its TestRail case by copying the ID, with no
  conversion. `400001` is also accepted and becomes `C400001`. The capital `C` is safe because Greenmail,
  Gmail and the reader all match recipients case-insensitively.
* **Read-only waiting.** The reader never marks, moves or deletes mail. It searches immediately (so an
  email that arrived before the wait started is found) and then polls for new messages only.
* **Exact recipient match, `To` by default.** Server search only narrows candidates; the final match is
  exact on the chosen header fields (`To`, `Cc`, `Bcc`, `Delivered-To`, `X-Original-To`).
* **Finding the button link.** The HTML link whose visible text is the button label (BeautifulSoup),
  with the plain-text part as fallback. If the HTML and text links differ, the test fails (possible
  phishing); an option turns that into a warning.
* **Our own sender plays the application.** It builds realistic emails from the two samples (same
  encodings, same token structure, a fresh token per email) and delivers them so they all land in the
  one `qa` inbox on Greenmail.
* **A shared local test broker** (port 8025) runs the sender and reader for every run on the machine,
  so they share one connection budget and one log. It starts automatically.
* **Retries only where they help.** Dropped connections and timeouts mid-session are retried; a server
  that is down, a wrong password or a TLS error fails at once.
* **Fail fast, and say why.** Every failure or skip carries a searchable keyword. An environment
  problem stops the job; a failing P0 test stops the whole chain; 5 failures in a row with the same
  keyword stop lower-priority runs.
* **Credentials only from the environment** (`.env` locally, secrets in CI); never in logs or reports.

### IMAP library: Python's built-in `imaplib`

**Why:** it is part of the standard library, so there is nothing to install and it is maintained
with Python itself. It speaks plain IMAP, so it works with any server (Gmail, Greenmail, others),
over plain, SSL or STARTTLS connections, and gives full control over the commands we send. It is
synchronous, which fits parallel pytest workers and ordinary blocking test code.

**Trade-offs, and how we handle them:**

| Disadvantage | Mitigation |
|---|---|
| Low-level API returning raw bytes | Our own small, unit-tested response parsing |
| Hides the original socket error (e.g. "connection reset") inside a generic `abort` message | Our wrapper keeps the original exception, so diagnostics name the real cause |
| Not thread-safe | One lock per connection in the shared service |
| No connection pooling, retries or reconnects | Implemented by us: retry with backoff, one connection per worker, connection limit |
| Folder names need a special encoding (modified UTF-7) | Encoded and decoded by our wrapper, so non-ASCII folder names work on every server |

**Alternatives considered:** `imapclient` (friendlier, but an extra dependency built on `imaplib`)
and `aioimaplib` (async, which does not match synchronous tests).

### Test broker client: `http.client` instead of `urllib`

**Why:** the broker only ever listens on the same machine (`127.0.0.1`). `urllib`, Python's usual HTTP
client, applies the `HTTP_PROXY` / `HTTPS_PROXY` environment variables that CI runners and company
machines often set; depending on `NO_PROXY`, a call to the local broker can then be sent to the proxy,
which cannot reach it, and fails with a misleading error. `http.client` opens a direct connection to
exactly the host and port given, every time. It is part of the standard library, so nothing is added.

**Trade-offs:** `http.client` does no redirects, cookies or proxy handling; the broker needs none of
them. Its errors (`HTTPException`) are turned into `OSError`, so the client reports
`TEST_BROKER_UNAVAILABLE` exactly as for a refused connection.

**Alternatives considered:** `urllib` with proxies switched off (works, but relies on remembering the
override everywhere), and `requests` (an extra dependency, and it also follows proxy settings).
The **Greenmail client keeps `urllib`**: Greenmail may run on another host, where following the
machine's proxy settings is the correct behaviour.

### `EmiError`: a tuple of the tools' error bases, not a base class

Tests catch any typed failure with `except EmiError as error:` and read `error.primary` /
`error.secondary`. `EmiError` is the tuple
`(ReaderError, SenderError, TestBrokerError, GreenmailError, ApiError)`, not a common base class.

**Why:**
- **Each tool owns its error base.** The reader, sender, broker and Greenmail client are standalone
  packages with their own unit jobs, and none of them may import `emi_api`. A shared base class would
  have to live in a package that all four import, which adds coupling the tool split exists to avoid.
- **A virtual base does not work.** Registering the tools' errors on an abstract `EmiError`
  (`EmiError.register(ReaderError)`) makes `isinstance` and `issubclass` true, but `except` skips
  `__subclasscheck__` and accepts only real subclasses. `except EmiError:` would quietly miss every
  tool error.
- **`except` accepts a tuple of classes**, so a tuple catches every family exactly, in both `service`
  and `inprocess` mode. The broker rebuilds the same classes on the client, so no class is missing.

**Trade-offs:**
- `EmiError` cannot be subclassed, and `isinstance(error, EmiError)` is the only membership check (it
  accepts the tuple too).
- New test families add their errors by subclassing `ApiError`, or by adding their own base to the
  tuple.
- The sender's errors have no secondary keywords. `keywords_of(error)` reads both keyword fields from
  any error and returns `("", ())` for an untyped one.
- The name breaks the constant naming pattern on purpose, because tests use it like a class. That is the
  one `invalid-name` exception below.

### Static analysis: pylint settings

The rules come from `CLAUDE.md` and `.claude/rules/code-style.md`; `.pylintrc` turns them into checks,
and every main pipeline run requires **10.00/10**.

| Setting | Value | Reason |
|---|---|---|
| Variable, argument and inline names | at least 3 characters, no allowed short names | Project rule: no single-letter names (`i`, `e`, `x`) in any scope |
| Attribute names | also `id` | Only for records that mirror a third-party format field by field (code-style §1) |
| Constants | `UPPER_SNAKE`, private ones with a leading `_` | Code-style §1 |
| Test names | `EMI_<TOOL>_<LAYER>_<6+ digit ID>_<behavior>`; the tool part may be several words (`TEST_BROKER`) | TestRail IDs in every test name (testing standards); one pattern for every tool |
| `max-line-length` | 120 | 110 forced awkward wrapping of typed signatures and keyword messages; 120 still fits side by side in a review |
| `max-args` / `max-positional-arguments` | 8 / 6 | Constructors take injectable clock, sleeper and factories so unit tests can simulate time and servers |
| `max-attributes` | 12 | Settings objects hold one field per documented option |
| `min-public-methods` | 0 | Data classes and protocols have no methods by design |

**Exceptions in the code**, each on the line it applies to:

| Exception | Where | Reason |
|---|---|---|
| `too-many-instance-attributes` | Settings classes, `WaitOptions`, `BatchRequest`, `ManifestEntry`, `BrokerService` | One field per documented option or report column; splitting them would scatter the option table |
| `too-many-arguments` | Constructors and wait/send calls with injectable test doubles | Same reason as `max-args` |
| `duplicate-code` | `emi_test_broker/errors.py` | Each tool owns its error type and shares code only through declared dependencies, so the error classes have the same shape on purpose |
| `invalid-name` | `do_GET`, `do_POST` in the broker server | Names fixed by the standard library's `BaseHTTPRequestHandler` |
| `invalid-name` | `EmiError` in `emi_api/errors.py` | A tuple used like a class in `except EmiError:` (see "`EmiError`" above) |
| `duplicate-code` | `emi_api/errors.py` | `ApiError` repeats the keyword-carrier shape of the other tools' errors, same reason as the broker's |
| `redefined-builtin` | `log_message(format, ...)` in the broker server | Signature fixed by `BaseHTTPRequestHandler` |
| `broad-exception-caught` | Broker request handler | The broker serves every run on the machine; one bad request must not stop it. The error is logged and returned |
| `consider-using-with` | Starting the broker process | The broker is detached on purpose and outlives the run that started it |
| `protected-access` | Two unit tests | A test reaches into an object to simulate a failure that cannot be triggered from outside |

## IMAP Connection Limit

All tests read one shared mailbox, so all of their IMAP connections count against a single
account. Real servers limit this: Gmail allows about **15** simultaneous IMAP connections per account.

| Server | Simultaneous IMAP connections | Runs at the default of 10 |
|---|---|---|
| Gmail | about **15** per account (all users of the account combined) | 1 |
| Greenmail (local Docker, v2.1.15) | **400** measured without failure (ceiling not reached) | 40 |

* `max_connections` (default **10**) caps the IMAP connections of one test run.
  Set it with `--emi-max-connections` or the `EMI_MAX_CONNECTIONS` environment variable.
* **Several runs against Gmail at once:** divide Gmail's 15 between every run and machine using
  the mailbox (for example 3 runs → `max_connections=5` each).
* **Against Greenmail:** one CI run plus several debug runs fit easily (up to 40 at the default).
* Each parallel worker uses one connection, and the harness never starts more workers than
  `max_connections`. If you ask for more with `-n`, it lowers the count and logs a warning.
* If several machines test against the same mailbox, split the server's limit between them:
  for example, 3 machines against Gmail → `max_connections=5` on each.
* If the server still refuses a connection as over the limit, the client retries with
  backoff and logs the keyword `READER_IMAP_CONNECTION_LIMIT`.
* Parallel runs never pick up each other's emails: every address carries the test ID and the
  run ID. No run purges the shared mailbox while others may be running.

## What Was Left Out

* **Separate repositories for the tools, the harness and the test cases.** Ideally each would be its own
  repository with versioned releases. A change to one would then be checked against the last green
  release of the other two before it is accepted, so a failure always points at the repository that
  changed. Everything is in one repository here because the project had one working day. The package
  boundaries, the one-way dependencies and the per-tool unit jobs keep a later split straightforward
  (design §2).
* **Gmail-specific features** (OAuth2 login `XOAUTH2`, Gmail search `X-GM-RAW`): the project targets
  Greenmail; any server, Gmail included, works through plain login and configuration.
* **IMAP `IDLE`** (push notifications): polling behaves the same on every server.
* **`<form>` and Outlook VML buttons**: link extraction is a pluggable chain, so they can be added.
* **Following redirects** behind a link, and **reading server filter rules**.
* **Several mail servers in parallel**: the design allows it; this project runs one sender and one
  reader against one server.
* **Starting Greenmail from tests**: tests check that it runs and skip with a clear message if not.
* **A relay for third-party applications** sending to Greenmail: only our own sender routes mail into
  the shared inbox.
* **Sharing one connection budget across machines**: each machine gets its own share (see above).
* **Folder names as plain UTF-8** (IMAP `UTF8=ACCEPT`): Greenmail does not support it, so it could not
  be verified; folder names use the modified UTF-7 encoding every IMAP server accepts.

## AI Tools Used

* **Claude Code** (Anthropic, Claude Opus model) was used as a collaborator for:
  * resolving requirements ambiguities and recording the decisions (the ambiguity register);
  * writing the design documents, the CI job description and this README;
  * small throwaway experiments against the local Greenmail server, whose results are recorded as
    evidence in the register (plus-address delivery, search behaviour, 400 simultaneous connections,
    1,000-email throughput, sample email structure).
* Every decision was made and approved by the author; the AI proposed options and recommendations.
* The AI worked under the project's instructions in `CLAUDE.md` and the rule files in `.claude/rules/`
  (see Project Structure); `CLAUDE_LOG.md` records the decisions and pylint scores phase by phase.

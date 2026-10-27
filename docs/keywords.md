<!--
SPDX-FileCopyrightText: 2026 Aleksandr Polskiy
SPDX-License-Identifier: Apache-2.0
-->
# Failure Keywords

> **Status: design.** Once the code exists, this file is generated from the code's keyword registry,
> and a unit test fails if code uses a keyword that is missing here (AMB-61).

Every test that does not pass carries one **primary** keyword (the cause) and any number of
**secondary** keywords (context). Search a report, a log or the CLI by keyword or by prefix:

```bash
python -m emi_harness.cli --keyword READER_IMAP_READ_TIMEOUT   # one keyword
python -m emi_harness.cli --keyword SENDER_                    # a whole tool
```

## Grammar

| Form | Meaning | Example |
|---|---|---|
| `<TOOL>_<PROTOCOL?>_<CAUSE>` | A **tool** failed. The tool comes first; a protocol never stands alone. | `SENDER_SMTP_CONNECTION_REFUSED` |
| `CASE_<CAUSE>` | The tools worked, but the application or the test case is wrong. | `CASE_LINK_NOT_FOUND` |
| `JOB_<CAUSE>` | A decision about the whole job. | `JOB_CIRCUIT_BREAKER_OPEN` |

Tool prefixes: `READER_`, `SENDER_`, `TEST_BROKER_`, `GREENMAIL_`, `API_`, `HARNESS_`.

**Outcomes:** **fail**: the test failed. **skip**: the test could not be judged (a skip is never a
pass). **skip + stop**: an environment (infrastructure) failure; the test is skipped and the job stops,
remaining tests skipped. **info**: secondary keyword only; it never decides the outcome.

## Reader (`READER_`)

| Keyword | Meaning | Outcome | First thing to check |
|---|---|---|---|
| `READER_IMAP_CONNECTION_REFUSED` | Nothing listens on the IMAP host:port | skip + stop | Is Greenmail running? Host and port settings |
| `READER_IMAP_DNS_FAILURE` | IMAP host name does not resolve | skip + stop | `EMI_IMAP_HOST` spelling |
| `READER_IMAP_CONNECTION_TIMEOUT` | Connect did not complete in time | skip + stop | Firewall, wrong port, host down |
| `READER_IMAP_READ_TIMEOUT` | Server stopped answering mid-session (retried) | skip + stop if retries did not cure it | Server load, network |
| `READER_IMAP_CONNECTION_DROPPED` | Connection closed mid-session (retried) | skip + stop if retries did not cure it | Server restarts, network |
| `READER_IMAP_SOCKET_WRITE_FAILED` | Sending a command failed on the socket (retried) | skip + stop if retries did not cure it | Network, server closed the connection |
| `READER_IMAP_TLS_HANDSHAKE_FAILED` | TLS failed (certificate, protocol) | skip + stop | `EMI_IMAP_SECURITY`, port, CA file |
| `READER_IMAP_AUTH_FAILED` | Reader login rejected | skip + stop | `EMI_IMAP_USER` / `EMI_IMAP_PASSWORD` |
| `READER_IMAP_PROTOCOL_ERROR` | Server answered `BAD` or unparsable data | skip + stop | Server type and version |
| `READER_IMAP_CONNECTION_LIMIT` | Server refused: too many connections (retried) | skip + stop if retries did not cure it | `max_connections`, other runs on the mailbox |
| `READER_IMAP_UIDVALIDITY_CHANGED` | Mailbox was recreated; full re-search done | info | Someone recreated or purged the mailbox |
| `READER_MAILBOX_NONE_ACCESSIBLE` | No configured account/folder could be opened | skip + stop | Accessibility matrix in the report |
| `READER_MAILBOX_EXPECTED_FOLDER_INACCESSIBLE` | The folder the test expects mail in cannot be opened | skip | Folder name, permissions |
| `READER_MAILBOX_FILTERED_TO_INACCESSIBLE` | Evidence that mail went to a folder we cannot read | skip + stop | Server filters, Junk folder access |
| `READER_MIME_DECODE_FAILED` | An email part could not be decoded | fail | The raw email attached to the report |
| `READER_PARSE_NO_HTML_PART` | Email has only plain text; text rule used | info | n/a |

## Sender (`SENDER_`)

| Keyword | Meaning | Outcome | First thing to check |
|---|---|---|---|
| `SENDER_SMTP_SERVER_UNREACHABLE` | SMTP host:port unreachable (exit 3) | skip + stop | Is Greenmail running? SMTP settings |
| `SENDER_SMTP_CONNECTION_REFUSED` | Nothing listens on the SMTP port | skip + stop | SMTP port |
| `SENDER_SMTP_CONNECTION_TIMEOUT` | Connect did not complete in time | skip + stop | Firewall, host down |
| `SENDER_SMTP_READ_TIMEOUT` | Server stopped answering mid-session (retried) | skip + stop if retries did not cure it | Server load |
| `SENDER_SMTP_CONNECTION_DROPPED` | Connection closed mid-session (retried) | skip + stop if retries did not cure it | Network |
| `SENDER_SMTP_SOCKET_WRITE_FAILED` | Writing to the socket failed (retried) | skip + stop if retries did not cure it | Network |
| `SENDER_SMTP_TLS_HANDSHAKE_FAILED` | TLS failed | skip + stop | `EMI_SMTP_SECURITY`, port |
| `SENDER_SMTP_AUTH_FAILED` | Sender login rejected | skip + stop | `EMI_SMTP_USER` / `EMI_SMTP_PASSWORD` |
| `SENDER_SMTP_RECIPIENT_REJECTED` | 5xx on `RCPT TO` | fail | Address format, server user `qa` exists |
| `SENDER_SMTP_MESSAGE_REJECTED` | 5xx on `DATA` | fail | Message size, server rules |
| `SENDER_IMAP_APPEND_FAILED` | `APPEND` into the mailbox failed | fail | Mailbox name, credentials |
| `SENDER_PARTIAL_FAILURE` | Some emails of a batch failed (exit 1); causes as secondary keywords | fail (skip if all causes are uncured network faults) | The secondary keywords |
| `SENDER_SEND_TIMEOUT` | One email took longer than its budget | fail | Server speed; raise the budget for slow servers |
| `SENDER_BATCH_TIMEOUT` | A batch took longer than its budget | fail | Batch size, concurrency |
| `SENDER_NEGATIVE_COUNT` | A negative count was given; nothing sent for it | info (exception logged) | The caller's input |

## Test broker (`TEST_BROKER_`)

| Keyword | Meaning | Outcome | First thing to check |
|---|---|---|---|
| `TEST_BROKER_UNAVAILABLE` | Broker not answering after one auto-start attempt | skip + stop | Broker log `logs/emi_test_broker_<date>.log`; a local firewall blocking port 8025 (README) |
| `TEST_BROKER_PORT_UNAVAILABLE` | Broker could not open its port | skip + stop | What holds port 8025; `EMI_TEST_BROKER_PORT` |
| `TEST_BROKER_VERSION_MISMATCH` | A broker of another version or config holds the port | skip + stop | Stop the old broker: `python -m emi_test_broker stop` |
| `TEST_BROKER_PURGE_REFUSED` | Purge refused: runs are active | info (purge tool) | The active run IDs in the message |

## Greenmail, API, harness

| Keyword | Meaning | Outcome | First thing to check |
|---|---|---|---|
| `GREENMAIL_NOT_READY` | Greenmail's REST readiness check failed (server down or starting); also secondary when only a mail port failed | skip + stop | README "Check that it is ready" |
| `GREENMAIL_SMTP_CONNECTION_FAILURE` | Greenmail answers, but an SMTP port (`smtp`, `smtps`) does not accept connections | skip + stop | Port published (`-p 3025:3025`), `EMI_SMTP_PORT` |
| `GREENMAIL_IMAP_CONNECTION_FAILURE` | Greenmail answers, but an IMAP port (`imap`, `imaps`) does not accept connections | skip + stop | Port published (`-p 3143:3143`, `-p 3993:3993`), `EMI_IMAP_PORT` |
| `GREENMAIL_API_REQUEST_FAILED` | A management call (users, purge, configuration) was answered with an error | fail (setup or tool) | The HTTP status and reason in the message |
| `API_ADDRESS_INVALID` | Test ID not a TestRail case ID (`C` + at least 6 digits; `400001` is accepted), run ID not ASCII `[a-z0-9_-]`, local part over 64 characters, or a malformed run ID | skip (condition error) | Test ID and run ID |
| `HARNESS_PROBE_EMAIL_NOT_RECEIVED` | Setup probe email did not arrive in a readable folder | skip + stop | Delivery routing, filters, accessibility matrix |
| `HARNESS_DOCKER_UNAVAILABLE` | Real-outage test needs Docker, not available | skip | Run locally or with `ci-debug-run` on Ubuntu |
| `HARNESS_DUPLICATE_TEST_ID` | Two tests share one TestRail ID (functional: per case, unit: per function) | collection fails | The test names listed in the message |

## Test case (`CASE_`)

| Keyword | Meaning | Outcome | First thing to check |
|---|---|---|---|
| `CASE_EMAIL_NOT_RECEIVED` | No matching email before the deadline (network fine) | fail | Did the application send it? Inventory |
| `CASE_EMAIL_COUNT_EXCEEDED` | More matching emails than expected | fail | Application resends, test expectations |
| `CASE_DUPLICATE_DELIVERY` | More than one match; newest used | info | Application resends |
| `CASE_LINK_NOT_FOUND` | No CTA link in the email | fail | CTA label and `url_pattern` |
| `CASE_LINK_AMBIGUOUS` | Several different CTA links | fail | Email template |
| `CASE_LINK_HTML_TEXT_MISMATCH` | HTML and text parts link to different URLs (possible phishing) | fail (or warn by option) | Both links in the report |
| `CASE_LINK_RECIPIENT_MISMATCH` | The link's token belongs to another address | fail | Application or test mixes recipients |
| `CASE_VERIFICATION_FAILED` | A step's `verify` (or a plain `assert`) was false, or a `pytest.raises` was not satisfied ("DID NOT RAISE") | fail | The verification named in the message |
| `CASE_NO_VERIFICATION` | The test would have passed without a single `step.verify` or passing `assert` (design §13.4) | fail | The test: add the checks it is meant to make |
| `CASE_UNEXPECTED_ERROR` | An untyped exception inside the test (its class is the secondary keyword) | fail | Test code or an unhandled application response |
| `CASE_STEP_TIMEOUT` | A step exceeded its time limit | fail | Step budget, slow operation |
| `CASE_TIMEOUT` | The test exceeded its time limit | fail | Test budget |
| `CASE_CLEANUP_FAILED` | Cleanup raised an error | info (never hides the original failure) | Cleanup log |
| `CASE_CONDITION_INVALID_INPUT` | Test parameters invalid (type or range) | skip | The field and value in the message |
| `CASE_CONDITION_NEGATIVE_COUNT` | Test asks to send or expect a negative number of emails | skip | Test parameters |
| `CASE_DEPENDENCY_SKIPPED` | A parent test failed or was skipped | skip | The parent named in the message |

## Job (`JOB_`)

| Keyword | Meaning | Outcome |
|---|---|---|
| `JOB_CRITICAL_FUNCTIONAL_FAILURE` | A P0 test failed | the P0 job fails (exit 1) but finishes; P1 and P2–P4 still run; only the failed test's dependents are skipped (AMB-65) |
| `JOB_CIRCUIT_BREAKER_OPEN` | 5 failures in a row with the same primary keyword (P2–P4, debug) | abort; remaining tests skipped |
| `JOB_PRIORITY_DOWNGRADED` | A P1 test moved to P2 for this run (P0 + P1 over 20%) | info |
| `JOB_PRIORITY_P0_OVERFLOW` | P0 alone over 20% of tests | collection fails |
| `JOB_TIMEOUT` | The job exceeded its time budget | abort; remaining tests skipped |
| `JOB_RESULT_MISSING` | A job selected a test but wrote no result for it (a crashed worker, a runner killed at its timeout) | the quality gate counts the test as not passed (skip), so lost results cannot raise the pass share |

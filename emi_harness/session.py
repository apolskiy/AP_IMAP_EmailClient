# SPDX-FileCopyrightText: 2026 Aleksandr Polskiy
# SPDX-License-Identifier: Apache-2.0
"""Session setup before the first functional test (design §9.6).

1. Greenmail readiness and port probes; 2. test broker health (auto-start in service mode); 3. and 4. the
**probe email**: one email per template kind to ``qa+C000000_<run id>``, waited for in the readable folders
(``probe_timeout``, default 15 s), which also proves the folders are accessible. Any failure is an
environment failure: every test is skipped with the keyword and the job stops.
"""

from emi_api import EmiApi, EmiRun, Health
from emi_harness.keywords import HarnessKeyword
from emi_harness.outcomes import CaseSkip
from emi_reader import EmailWaitError
from emi_sender import BatchRequest, Kind

PROBE_CASE = "C000000"


def check_health(health: Health) -> None:
    """Steps 1 and 2: Greenmail and the broker.

    Args:
        health: ``api.health()``.

    Raises:
        CaseSkip: An infrastructure skip with the health keywords.
    """
    if not health.ready:
        primary, secondary = health.keywords
        problems = "; ".join(f"{item.component}: {item.message}" for item in health.problems)
        raise CaseSkip(f"session setup: {problems}", primary, secondary=secondary, step="SETUP",
                       infrastructure=True)


def send_probe(api: EmiApi, timeout: float) -> None:
    """Steps 3 and 4: one email of each kind must arrive in a readable folder.

    Args:
        api: The session's API.
        timeout: Seconds to wait for the probe emails.

    Raises:
        CaseSkip: ``HARNESS_PROBE_EMAIL_NOT_RECEIVED`` (with the reader's keywords), or the keyword of the
            delivery or connection failure, as an infrastructure skip.
    """
    run = EmiRun(PROBE_CASE, api.run_id, api.settings.smtp.base_mailbox)
    address = run.address()
    kinds = tuple(str(kind) for kind in Kind)
    try:
        manifest = api.sender.send_batch(BatchRequest(addresses=(address,), counts=dict.fromkeys(kinds, 1)),
                                         marker=run.marker)
        primary, secondary = manifest.keywords
        if primary:
            raise CaseSkip(f"session setup: probe email not delivered to {address}", primary, secondary=secondary,
                           step="SETUP", infrastructure=True)
        api.reader.wait_for_emails(address, count=len(kinds), timeout=timeout)
    except EmailWaitError as error:
        raise CaseSkip(f"session setup: probe emails to {address} not received within {timeout:g} s: {error}",
                       HarnessKeyword.PROBE_EMAIL_NOT_RECEIVED, secondary=(error.primary, *error.secondary),
                       step="SETUP", infrastructure=True) from error
    except CaseSkip:
        raise
    except Exception as error:  # pylint: disable=broad-exception-caught
        # Any other failure here is an environment problem; it keeps its keyword when it has one.
        primary = str(getattr(error, "primary", "") or HarnessKeyword.PROBE_EMAIL_NOT_RECEIVED)
        secondary = tuple(getattr(error, "secondary", ()) or ())
        raise CaseSkip(f"session setup: probe email failed: {error}", primary, secondary=secondary, step="SETUP",
                       infrastructure=True) from error


def session_setup(api: EmiApi, *, probe: bool, probe_timeout: float) -> None:
    """Run the whole session setup.

    Args:
        api: The session's API.
        probe: Whether to send the probe email.
        probe_timeout: Seconds to wait for it.

    Raises:
        CaseSkip: On any environment failure.
    """
    check_health(api.health())
    if probe:
        send_probe(api, probe_timeout)

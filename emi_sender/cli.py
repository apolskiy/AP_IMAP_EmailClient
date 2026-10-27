# SPDX-FileCopyrightText: 2026 Aleksandr Polskiy
# SPDX-License-Identifier: Apache-2.0
"""Command line: ``python -m emi_sender`` (design §6; exit codes in §6.6).

The settings flags are generated from the sender's option table (``emi_sender.settings.OPTIONS``,
AMB-81). Precedence: CLI option > real environment variable > ``.env`` > built-in default. Passwords
have no CLI option; they come only from ``EMI_SMTP_PASSWORD`` / ``EMI_IMAP_PASSWORD``.
"""

import argparse
import json
import logging
import os
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Protocol

from emi_config import add_arguments, from_command_line
from emi_sender.batch import BatchRequest, Order, Preset
from emi_sender.compose import Marker, parse_fields
from emi_sender.errors import InvalidInputError
from emi_sender.sender import EXIT_INVALID, EXIT_UNREACHABLE, Manifest, Sender
from emi_sender.settings import OPTIONS, SenderSettings, sender_settings_from

_LOGGER = logging.getLogger("emi_sender")


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser.

    Returns:
        The parser.
    """
    parser = argparse.ArgumentParser(
        prog="python -m emi_sender",
        description="Send activation, invitation and generic test emails to the shared mailbox.",
    )
    parser.add_argument("--to", dest="addresses", action="append", default=None, metavar="ADDRESS",
                        help="recipient address; repeat for several")
    for kind in ("activation", "invitation", "generic"):
        parser.add_argument(f"--{kind}", type=int, default=None, metavar="N",
                            help=f"number of {kind} emails (negative sends none and is logged)")
    parser.add_argument("--batch-file", type=Path, help="JSON batch file; options given here override it")
    parser.add_argument("--preset", choices=[str(item) for item in Preset], help="distribution over addresses")
    parser.add_argument("--order", choices=[str(item) for item in Order], help="send order")
    parser.add_argument("--seed", type=int, help="seed for random choices (always logged)")
    parser.add_argument("--concurrency", type=int, help="parallel connections, 1-32")
    parser.add_argument("--jitter-ms", type=int, help="random delay between sends, 0-60000")
    parser.add_argument("--field", metavar="FIELDS",
                        help="recipient field: to (default), cc, bcc, or a combination such as to,cc")
    parser.add_argument("--test-id", default="", help="TestRail case ID marker (C400001; 400001 accepted)")
    parser.add_argument("--run-id", default="", help="marker run ID")
    parser.add_argument("--output", type=Path, help="also save the manifest JSON here")
    parser.add_argument("--via-broker", action="store_true",
                        help="send through the test broker (started if needed), sharing its connections and log")
    add_arguments(parser.add_argument_group("settings (also EMI_* variables or .env)").add_argument, OPTIONS)
    return parser


def _settings(args: argparse.Namespace) -> SenderSettings:
    """Build delivery settings from the generated flags, the environment and ``.env``."""
    return sender_settings_from(from_command_line(args, OPTIONS, os.environ))


def _request(args: argparse.Namespace) -> BatchRequest:
    """Merge the batch file and the options into a request."""
    batch: dict[str, Any] = {}
    if args.batch_file is not None:
        try:
            batch = json.loads(args.batch_file.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise InvalidInputError(f"batch file {args.batch_file}: {error}",
                                    "CASE_CONDITION_INVALID_INPUT") from error
    counts = dict(batch.get("counts", {}))
    for kind in ("activation", "invitation", "generic"):
        if getattr(args, kind) is not None:
            counts[kind] = getattr(args, kind)
    addresses = args.addresses or batch.get("recipients") or []
    return BatchRequest(
        addresses=tuple(addresses), counts=counts,
        preset=Preset(args.preset or batch.get("preset", "all_to_one")),
        explicit=batch.get("explicit"),
        order=Order(args.order or batch.get("order", "fixed")),
        seed=args.seed if args.seed is not None else batch.get("seed"),
        recipient_field=parse_fields(args.field or batch.get("field", "to")),
        concurrency=args.concurrency if args.concurrency is not None else batch.get("concurrency", 1),
        jitter_ms=args.jitter_ms if args.jitter_ms is not None else batch.get("delay_ms", 0),
    )


class _BatchSender(Protocol):  # pylint: disable=too-few-public-methods
    """What sends a batch: the sender itself, or the test broker's client."""

    def send_batch(self, request: BatchRequest, *, marker: Marker | None = None) -> Manifest:
        """Send the batch and return its manifest."""


def _broker(args: argparse.Namespace) -> _BatchSender:
    """The running test broker's client, started if needed, with this command line's settings.

    The broker package is imported only here, so the sender runs without it (AMB-84).

    Raises:
        TestBrokerError: If no usable broker can be reached or started.
    """
    from emi_test_broker.client import BrokerClient  # pylint: disable=import-outside-toplevel
    from emi_test_broker.settings import OPTIONS as BROKER_OPTIONS  # pylint: disable=import-outside-toplevel
    from emi_test_broker.settings import broker_settings_from  # pylint: disable=import-outside-toplevel
    client = BrokerClient(broker_settings_from(from_command_line(args, BROKER_OPTIONS, os.environ)))
    client.ensure_running()
    return client


def main(argv: Sequence[str] | None = None, sender: Sender | None = None,
         broker: _BatchSender | None = None) -> int:
    """Run the CLI.

    Args:
        argv: Arguments (defaults to ``sys.argv[1:]``).
        sender: Sender to use (injected in unit tests).
        broker: Broker client used with ``--via-broker`` (injected in unit tests).

    Returns:
        Exit code: 0 all sent, 1 some failed, 2 invalid input, 3 server or broker unreachable.
    """
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        stream=sys.stderr)
    args = build_parser().parse_args(argv)
    try:
        marker = Marker(test_id=args.test_id, run_id=args.run_id)
        request = _request(args)
        if args.via_broker:
            delivery: _BatchSender = broker or _broker(args)
        else:
            delivery = sender or Sender(_settings(args))
        manifest = delivery.send_batch(request, marker=marker)
    except (InvalidInputError, ValueError) as error:
        keyword = getattr(error, "primary", "CASE_CONDITION_INVALID_INPUT")
        _LOGGER.error("%s: %s", keyword, error)
        return EXIT_INVALID
    except Exception as error:  # pylint: disable=broad-exception-caught
        if not type(error).__module__.startswith("emi_test_broker"):  # matched by module: never imported here
            raise
        _LOGGER.error("%s: %s", getattr(error, "primary", ""), error)
        return EXIT_UNREACHABLE
    print(manifest.to_json())
    if args.output is not None:
        manifest.save(args.output)
    return manifest.exit_code

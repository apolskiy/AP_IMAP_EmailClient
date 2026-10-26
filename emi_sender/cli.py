# SPDX-FileCopyrightText: 2026 Aleksandr Polskiy
# SPDX-License-Identifier: Apache-2.0
"""Command line: ``python -m emi_sender`` (design §6; exit codes in §6.6).

Settings precedence: CLI option > real environment variable > ``.env`` > built-in default.
Passwords have no CLI option; they come only from ``EMI_SMTP_PASSWORD`` / ``EMI_IMAP_PASSWORD``.
"""

import argparse
import json
import logging
import os
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from emi_sender.batch import BatchRequest, Order, Preset
from emi_sender.compose import Field, Marker
from emi_sender.errors import InvalidInputError
from emi_sender.sender import EXIT_INVALID, Sender
from emi_sender.settings import GREENMAIL_TEST_PASSWORD, SenderSettings, Security, Transport

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
    parser.add_argument("--field", choices=[str(item) for item in Field], help="recipient field (default to)")
    parser.add_argument("--transport", choices=[str(item) for item in Transport], help="smtp or append")
    parser.add_argument("--host", help="mail server host (env EMI_SMTP_HOST)")
    parser.add_argument("--smtp-port", type=int, help="SMTP port (env EMI_SMTP_PORT)")
    parser.add_argument("--imap-port", type=int, help="IMAP port for append (env EMI_IMAP_PORT)")
    parser.add_argument("--security", choices=[str(item) for item in Security], help="env EMI_SMTP_SECURITY")
    parser.add_argument("--base-mailbox", help="envelope recipient: the shared mailbox (default qa@example.com)")
    parser.add_argument("--test-id", default="", help="TestRail case ID marker (C400001; 400001 accepted)")
    parser.add_argument("--run-id", default="", help="marker run ID")
    parser.add_argument("--output", type=Path, help="also save the manifest JSON here")
    return parser


def _setting(cli_value: Any, env_name: str, batch: dict[str, Any], key: str, default: Any) -> Any:
    """Resolve one value: CLI option > environment (incl. ``.env``) > batch file > default."""
    if cli_value is not None:
        return cli_value
    if os.environ.get(env_name):
        return os.environ[env_name]
    return batch.get(key, default)


def _settings(args: argparse.Namespace) -> SenderSettings:
    """Build delivery settings from options and environment."""
    transport = Transport(_setting(args.transport, "EMI_SENDER_TRANSPORT", {}, "", "smtp"))
    password_env = "EMI_SMTP_PASSWORD" if transport is Transport.SMTP else "EMI_IMAP_PASSWORD"
    smtp_port = _setting(args.smtp_port, "EMI_SMTP_PORT", {}, "", None)
    imap_port = _setting(args.imap_port, "EMI_IMAP_PORT", {}, "", None)
    return SenderSettings(
        transport=transport,
        host=_setting(args.host, "EMI_SMTP_HOST", {}, "", "127.0.0.1"),
        smtp_port=int(smtp_port) if smtp_port is not None else None,
        imap_port=int(imap_port) if imap_port is not None else None,
        security=Security(_setting(args.security, "EMI_SMTP_SECURITY", {}, "", "plain")),
        username=os.environ.get("EMI_SMTP_USER") or "qa",
        password=os.environ.get(password_env) or GREENMAIL_TEST_PASSWORD,
        smtp_login=bool(os.environ.get("EMI_SMTP_PASSWORD")),
        base_mailbox=args.base_mailbox or "qa@example.com",
    )


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
        recipient_field=Field(args.field or batch.get("field", "to")),
        concurrency=args.concurrency if args.concurrency is not None else batch.get("concurrency", 1),
        jitter_ms=args.jitter_ms if args.jitter_ms is not None else batch.get("delay_ms", 0),
    )


def main(argv: Sequence[str] | None = None, sender: Sender | None = None) -> int:
    """Run the CLI.

    Args:
        argv: Arguments (defaults to ``sys.argv[1:]``).
        sender: Sender to use (injected in unit tests).

    Returns:
        Exit code: 0 all sent, 1 some failed, 2 invalid input, 3 server unreachable.
    """
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        stream=sys.stderr)
    load_dotenv(override=False)  # real environment variables win over .env
    args = build_parser().parse_args(argv)
    try:
        marker = Marker(test_id=args.test_id, run_id=args.run_id)
        request = _request(args)
        sender = sender or Sender(_settings(args))
        manifest = sender.send_batch(request, marker=marker)
    except (InvalidInputError, ValueError) as error:
        keyword = getattr(error, "primary", "CASE_CONDITION_INVALID_INPUT")
        _LOGGER.error("%s: %s", keyword, error)
        return EXIT_INVALID
    print(manifest.to_json())
    if args.output is not None:
        manifest.save(args.output)
    return manifest.exit_code

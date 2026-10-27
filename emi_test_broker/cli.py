# SPDX-FileCopyrightText: 2026 Aleksandr Polskiy
# SPDX-License-Identifier: Apache-2.0
"""Command line: ``python -m emi_test_broker start|serve|stop|status|purge`` (design §7).

The settings flags are generated from the broker's option table (``emi_test_broker.settings.OPTIONS``:
the reader's and sender's rows plus the broker's own, AMB-81). Precedence: CLI option > real environment
variable > ``.env`` > built-in default; passwords come only from the environment or ``.env``.

Exit codes: 0 done, 1 purge refused (runs active; with ``--run-id``, that run), 2 invalid input, 3 broker
unavailable (not answering, port taken, other version on the port).
"""

import argparse
import json
import logging
import os
import re
import sys
from collections.abc import Sequence
from pathlib import Path

from emi_config import add_arguments, from_command_line, read_dotenv
from emi_reader import ReaderError
from emi_test_broker.broker_log import LOGGER_NAME, configure
from emi_test_broker.client import BrokerClient, identity_problem
from emi_test_broker.errors import BrokerUnavailableError, PurgeRefusedError
from emi_test_broker.keywords import TestBrokerKeyword
from emi_test_broker.purge import PurgeRequest, run_of
from emi_test_broker.server import bind, serve
from emi_test_broker.service import BrokerService
from emi_test_broker.settings import OPTIONS, BrokerSettings, broker_settings_from, settings_from_json

EXIT_OK = 0
EXIT_REFUSED = 1
EXIT_INVALID = 2
EXIT_UNAVAILABLE = 3
_DAYS = re.compile(r"^(\d+)d?$")


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser.

    Returns:
        The parser.
    """
    parser = argparse.ArgumentParser(prog="python -m emi_test_broker", description="EMI shared test broker")
    add_arguments(parser.add_argument_group("settings (also EMI_* variables or .env)").add_argument, OPTIONS)
    parser.add_argument("--state-dir", type=Path, help="directory of the broker's state file (default: temp)")
    parser.add_argument("--settings-file", type=Path,
                        help="settings written by an auto-start (passwords from the environment)")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("start", help="start the broker in the background (reuse a running one)")
    commands.add_parser("serve", help="run the broker in the foreground")
    commands.add_parser("stop", help="stop the running broker")
    commands.add_parser("status", help="print the running broker's health")
    purge = commands.add_parser("purge", help="delete EMI test mail from the shared mailbox")
    purge.add_argument("--dry-run", action="store_true", help="count only")
    purge.add_argument("--older-than", help="only mail older than N days, e.g. 7d")
    purge.add_argument("--all", action="store_true", dest="everything", help="every email, not only EMI mail")
    purge.add_argument("--run-id", help="only this run's mail, every worker's (other runs may be active)")
    return parser


def _older_than_days(text: str | None) -> int | None:
    """Parse ``--older-than`` (days; IMAP dates are day-granular)."""
    if text is None:
        return None
    match = _DAYS.match(text.strip().lower())
    if not match:
        raise ValueError(f"--older-than must be a number of days such as 7d, got {text!r}")
    return int(match.group(1))


def _serve(settings: BrokerSettings) -> int:
    """Run in the foreground: reuse a matching broker, refuse a foreign one, else bind and serve."""
    logger = configure(settings.log_dir)
    client = BrokerClient(settings)
    health = client.health()
    if health is not None:
        problem = identity_problem(health, settings)
        if not problem:
            logger.info("broker already running on port %d (pid %s); reusing it", settings.port, health.get("pid"))
            return EXIT_OK
        keyword = (TestBrokerKeyword.VERSION_MISMATCH if health.get("service") == "emi_test_broker"
                   else TestBrokerKeyword.PORT_UNAVAILABLE)
        logger.error("%s port %d: %s", keyword, settings.port, problem)
        return EXIT_UNAVAILABLE
    try:
        server, state = bind(BrokerService(settings))
    except BrokerUnavailableError:
        racing = client.health()  # another start may have won the bind a moment ago
        if racing is not None and not identity_problem(racing, settings):
            logger.info("another start won the port %d; reusing it", settings.port)
            return EXIT_OK
        return EXIT_UNAVAILABLE
    serve(server, state)
    return EXIT_OK


def _purge(client: BrokerClient, args: argparse.Namespace) -> int:
    """Run a purge through the broker so the active-run check applies."""
    older_than_days = _older_than_days(args.older_than)  # invalid input fails before any broker is started
    if args.run_id:
        PurgeRequest(older_than_days=older_than_days, everything=args.everything, run=run_of(args.run_id))
    client.ensure_running()
    try:
        report = client.purge(dry_run=args.dry_run, older_than_days=older_than_days, everything=args.everything,
                              run_id=args.run_id)
    except PurgeRefusedError as error:
        print(f"{error.primary}: {error}", file=sys.stderr)
        return EXIT_REFUSED
    print(json.dumps(report, indent=2))
    return EXIT_OK


def main(argv: Sequence[str] | None = None) -> int:
    """Run the CLI.

    Args:
        argv: Arguments; ``sys.argv[1:]`` by default.

    Returns:
        The exit code.
    """
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    args = build_parser().parse_args(argv)
    try:
        if args.settings_file:
            # Passwords: the environment the starter passed, else .env; a real variable wins.
            known = {key: value for key, value in read_dotenv().items() if value is not None}
            settings = settings_from_json(json.loads(args.settings_file.read_text(encoding="utf-8")),
                                          {**known, **os.environ})
        else:
            settings = broker_settings_from(from_command_line(args, OPTIONS, os.environ), state_dir=args.state_dir)
        client = BrokerClient(settings)
        if args.command == "serve":
            return _serve(settings)
        if args.command == "start":
            print(json.dumps(client.ensure_running(), indent=2))
            return EXIT_OK
        if args.command == "status":
            health = client.health()
            print(json.dumps(health, indent=2) if health else f"no broker on {settings.base_url}")
            return EXIT_OK if health else EXIT_UNAVAILABLE
        if args.command == "stop":
            stopped = client.stop()
            print("stopping" if stopped else f"no broker on {settings.base_url}")
            return EXIT_OK if stopped else EXIT_UNAVAILABLE
        return _purge(client, args)
    except ValueError as error:
        print(f"invalid input: {error}", file=sys.stderr)
        return EXIT_INVALID
    except (BrokerUnavailableError, ReaderError) as error:
        logging.getLogger(LOGGER_NAME).error("%s: %s", error.primary, error)
        print(f"{error.primary}: {error}", file=sys.stderr)
        return EXIT_UNAVAILABLE

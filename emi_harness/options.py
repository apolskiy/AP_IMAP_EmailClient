# SPDX-FileCopyrightText: 2026 Aleksandr Polskiy
# SPDX-License-Identifier: Apache-2.0
"""The harness option table: the API's table plus the harness's own rows (design §3, AMB-81).

The rows and the precedence rules live in :mod:`emi_config`; each tool declares its own rows and the API
merges them. Here every option carries the ``emi-`` prefix (``--emi-imap-host``), because the harness CLI
and the pytest plugin share pytest's option namespace. Both are generated from this table, and a unit test
checks they match. Precedence, highest first: **argument in code > CLI option > real environment variable
> ``.env`` file > built-in default**.
"""

import argparse
from collections.abc import Callable, Mapping
from typing import Any

from emi_api import EmiSettings
from emi_api.settings import OPTIONS as API_OPTIONS
from emi_config import REDACTED, Option, Resolved, Source, merge, read_dotenv
from emi_config import add_arguments as _add_arguments
from emi_config import cli_values as _cli_values
from emi_config import resolve as _resolve
from emi_harness.faults import FAULT_CHOICES, NO_FAULT

PREFIX = "emi-"

OPTIONS: tuple[Option, ...] = merge(API_OPTIONS, (
    Option(name="probe_timeout", env="EMI_PROBE_TIMEOUT", kind="float", default=15.0, minimum=1, maximum=3600,
           help="seconds the setup probe email may take"),
    Option(name="probe", env="EMI_PROBE", kind="bool", default=True,
           help="send the setup probe email before the first test (on/off)"),
    Option(name="step_timeout", env="EMI_STEP_TIMEOUT", kind="float", default=30.0, minimum=1, maximum=3600,
           help="default seconds per step"),
    Option(name="test_timeout", env="EMI_TEST_TIMEOUT", kind="float", default=90.0, minimum=1, maximum=3600,
           help="default seconds per test"),
    Option(name="job_timeout", env="EMI_JOB_TIMEOUT", kind="float", default=0.0, minimum=0, maximum=86400,
           help="job time budget in seconds (0: P0 300, P1 600, P2/P3/P4/P2-P4 3600, debug 900, all unlimited)"),
    Option(name="run_id", env="EMI_RUN_ID", kind="str", default="",
           help="run ID shared by every job of a run (default: a new one)"),
    Option(name="reports_dir", env="EMI_REPORTS_DIR", kind="str", default="reports", help="report directory"),
    Option(name="fault", env="EMI_FAULT", kind="choice", default=NO_FAULT, choices=FAULT_CHOICES,
           help="named fault for test-validity runs (design §13.4); results are never gate results"),
    Option(name="delete_own_mail", env="EMI_DELETE_OWN_MAIL", kind="bool", default=False,
           help="at the end of the job, delete this run's mail and nothing else (on/off, AMB-79)"),
))
OPTIONS_BY_NAME: dict[str, Option] = {option.name: option for option in OPTIONS}

__all__ = ["OPTIONS", "OPTIONS_BY_NAME", "PREFIX", "REDACTED", "Resolved", "Source", "add_arguments",
           "add_pytest_options", "cli_values", "api_settings", "pass_through", "read_dotenv", "resolve"]


def resolve(
    cli: Mapping[str, Any] | None = None,
    environ: Mapping[str, str] | None = None,
    dotenv: Mapping[str, str | None] | None = None,
    arguments: Mapping[str, Any] | None = None,
) -> Resolved:
    """Resolve every harness option by precedence.

    Args:
        cli: name -> value given on the command line (None = not given).
        environ: Real environment variables.
        dotenv: Values from the ``.env`` file.
        arguments: name -> value given in code (highest precedence).

    Returns:
        Values and sources.

    Raises:
        ValueError: For an unknown argument or a value of the wrong type or range.
    """
    return _resolve(OPTIONS, cli, environ, dotenv, arguments)


def api_settings(resolved: Resolved) -> EmiSettings:
    """The API settings resolved harness options describe.

    Args:
        resolved: Values of :data:`OPTIONS`.

    Returns:
        Settings for :class:`emi_api.EmiApi`.

    Raises:
        ValueError: If a combination is invalid.
    """
    return EmiSettings.from_options(resolved)


def add_arguments(parser: argparse.ArgumentParser | argparse._ArgumentGroup) -> None:
    """Add every non-secret option to an argparse parser (harness CLI).

    Args:
        parser: Parser or argument group.
    """
    _add_arguments(parser.add_argument, OPTIONS, PREFIX)


def add_pytest_options(add: Callable[..., Any]) -> None:
    """Add every non-secret option to pytest (``parser.getgroup("emi").addoption``).

    Args:
        add: The group's ``addoption``.
    """
    _add_arguments(add, OPTIONS, PREFIX)


def cli_values(namespace: Any) -> dict[str, Any]:
    """Option values given on a command line (argparse namespace or pytest ``config.option``).

    Args:
        namespace: Object with ``emi_<name>`` attributes.

    Returns:
        name -> value, for options that were given.
    """
    return _cli_values(namespace, OPTIONS, PREFIX)


def pass_through(namespace: Any) -> list[str]:
    """Rebuild the ``--emi-*`` flags given to the harness CLI for pytest, unchanged.

    Args:
        namespace: The CLI's parsed arguments.

    Returns:
        Flags and values.
    """
    flags: list[str] = []
    for name, value in cli_values(namespace).items():
        flags.extend((OPTIONS_BY_NAME[name].flag(PREFIX), str(value)))
    return flags

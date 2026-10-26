# SPDX-FileCopyrightText: 2026 Aleksandr Polskiy
# SPDX-License-Identifier: Apache-2.0
"""The one option table (design §3).

Each option is defined once: name, ``EMI_*`` variable, type, default, range, help text and secret flag. The
pytest options (``--emi-*``) and the harness CLI's argparse options are both generated from it, and a unit
test checks they match. Precedence, highest first: **argument in code > CLI option > real environment
variable > ``.env`` file > built-in default**. Secret options (passwords) have no CLI flag and are redacted
whenever settings are shown; every value is reported together with its source.
"""

import argparse
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from dotenv import dotenv_values

from emi_api import ClientMode, EmiSettings
from emi_greenmail import GreenmailSettings
from emi_reader import ImapSettings
from emi_reader import Security as ImapSecurity
from emi_reader.settings import GREENMAIL_TEST_PASSWORD
from emi_sender import SenderSettings, Transport
from emi_sender import Security as SmtpSecurity
from emi_test_broker import BrokerSettings

REDACTED = "***"


class Source(StrEnum):
    """Where a value came from (precedence order, highest first)."""

    ARGUMENT = "argument"
    CLI = "cli"
    ENV = "env"
    DOTENV = ".env"
    DEFAULT = "default"


@dataclass(frozen=True, kw_only=True)
class Option:  # pylint: disable=too-many-instance-attributes
    """One documented option.

    Attributes:
        name: Python name (``imap_host``); the CLI flag is ``--emi-imap-host``.
        env: Environment variable (``EMI_IMAP_HOST``).
        kind: ``str``, ``int``, ``float``, ``bool`` or ``choice``.
        default: Built-in default.
        help: Help text.
        minimum: Smallest allowed number.
        maximum: Largest allowed number.
        choices: Allowed values of a ``choice``.
        secret: Never on the command line; redacted when shown.
    """

    name: str
    env: str
    kind: str
    default: Any
    help: str
    minimum: float | None = None
    maximum: float | None = None
    choices: tuple[str, ...] = ()
    secret: bool = False

    @property
    def flag(self) -> str:
        """The command-line flag."""
        return "--emi-" + self.name.replace("_", "-")

    @property
    def dest(self) -> str:
        """The argparse / pytest destination."""
        return "emi_" + self.name

    def convert(self, raw: Any) -> Any:
        """Turn a raw value (text from CLI, env or ``.env``) into the option's type and check its range.

        Args:
            raw: The raw value.

        Returns:
            The typed value.

        Raises:
            ValueError: If the value has the wrong type or is out of range.
        """
        value = _CONVERTERS[self.kind](self, raw)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            if (self.minimum is not None and value < self.minimum) or (
                    self.maximum is not None and value > self.maximum):
                raise ValueError(f"{self.flag} / {self.env} must be {self.minimum:g}-{self.maximum:g}, got {raw!r}")
        return value


def _text(option: Option, raw: Any) -> str:
    """Text option."""
    del option
    return str(raw).strip()


def _integer(option: Option, raw: Any) -> int | None:
    """Integer option; empty text means "not set" for options without a default."""
    text = str(raw).strip()
    if text == "" and option.default is None:
        return None
    try:
        return int(text)
    except ValueError as error:
        raise ValueError(f"{option.flag} / {option.env} must be a whole number, got {raw!r}") from error


def _decimal(option: Option, raw: Any) -> float:
    """Number option."""
    try:
        return float(str(raw).strip())
    except ValueError as error:
        raise ValueError(f"{option.flag} / {option.env} must be a number, got {raw!r}") from error


def _boolean(option: Option, raw: Any) -> bool:
    """On/off option."""
    if isinstance(raw, bool):
        return raw
    text = str(raw).strip().lower()
    if text in ("1", "true", "yes", "on"):
        return True
    if text in ("0", "false", "no", "off"):
        return False
    raise ValueError(f"{option.flag} / {option.env} must be on/off, got {raw!r}")


def _choice(option: Option, raw: Any) -> str:
    """One of a fixed set."""
    text = str(raw).strip().lower()
    if text not in option.choices:
        raise ValueError(f"{option.flag} / {option.env} must be one of {', '.join(option.choices)}, got {raw!r}")
    return text


_CONVERTERS: dict[str, Callable[[Option, Any], Any]] = {
    "str": _text, "int": _integer, "float": _decimal, "bool": _boolean, "choice": _choice}
_SECURITY = ("plain", "ssl", "starttls")

OPTIONS: tuple[Option, ...] = (
    Option(name="client_mode", env="EMI_CLIENT_MODE", kind="choice", default="service",
           choices=("service", "inprocess"), help="send and wait through the test broker or in this process"),
    Option(name="imap_host", env="EMI_IMAP_HOST", kind="str", default="127.0.0.1", help="IMAP server host"),
    Option(name="imap_port", env="EMI_IMAP_PORT", kind="int", default=None, minimum=1, maximum=65535,
           help="IMAP port (default per security: 3143 plain, 3993 ssl)"),
    Option(name="imap_security", env="EMI_IMAP_SECURITY", kind="choice", default="plain", choices=_SECURITY,
           help="IMAP connection security"),
    Option(name="imap_user", env="EMI_IMAP_USER", kind="str", default="qa", help="IMAP login"),
    Option(name="imap_password", env="EMI_IMAP_PASSWORD", kind="str", default=GREENMAIL_TEST_PASSWORD,
           secret=True, help="IMAP password (env or .env only)"),
    Option(name="imap_folders", env="EMI_IMAP_FOLDERS", kind="str", default="INBOX",
           help="comma-separated folders the reader searches"),
    Option(name="smtp_host", env="EMI_SMTP_HOST", kind="str", default="127.0.0.1", help="SMTP server host"),
    Option(name="smtp_port", env="EMI_SMTP_PORT", kind="int", default=None, minimum=1, maximum=65535,
           help="SMTP port (default per security: 3025 plain, 3465 ssl)"),
    Option(name="smtp_security", env="EMI_SMTP_SECURITY", kind="choice", default="plain", choices=_SECURITY,
           help="SMTP connection security"),
    Option(name="smtp_user", env="EMI_SMTP_USER", kind="str", default="qa", help="SMTP login"),
    Option(name="smtp_password", env="EMI_SMTP_PASSWORD", kind="str", default=GREENMAIL_TEST_PASSWORD,
           secret=True, help="SMTP password (env or .env only); setting it turns SMTP login on"),
    Option(name="sender_transport", env="EMI_SENDER_TRANSPORT", kind="choice", default="smtp",
           choices=("smtp", "append"), help="how the sender delivers: SMTP or IMAP APPEND"),
    Option(name="base_mailbox", env="EMI_BASE_MAILBOX", kind="str", default="qa@example.com",
           help="the shared mailbox every test address is built on"),
    Option(name="greenmail_host", env="EMI_GREENMAIL_HOST", kind="str", default="",
           help="Greenmail host (default: the IMAP host)"),
    Option(name="greenmail_api_port", env="EMI_GREENMAIL_API_PORT", kind="int", default=8080, minimum=1,
           maximum=65535, help="Greenmail REST API port"),
    Option(name="max_connections", env="EMI_MAX_CONNECTIONS", kind="int", default=10, minimum=1, maximum=100,
           help="IMAP/SMTP connections per host (Gmail allows about 15 per account)"),
    Option(name="test_broker_port", env="EMI_TEST_BROKER_PORT", kind="int", default=8025, minimum=1,
           maximum=65535, help="test broker port"),
    Option(name="connect_timeout", env="EMI_CONNECT_TIMEOUT", kind="float", default=10.0, minimum=1,
           maximum=3600, help="seconds to open an IMAP/SMTP connection"),
    Option(name="read_timeout", env="EMI_READ_TIMEOUT", kind="float", default=15.0, minimum=1, maximum=3600,
           help="seconds for one IMAP/SMTP reply"),
    Option(name="wait_timeout", env="EMI_WAIT_TIMEOUT", kind="float", default=60.0, minimum=1, maximum=3600,
           help="default seconds a wait for an email may take"),
    Option(name="probe_timeout", env="EMI_PROBE_TIMEOUT", kind="float", default=15.0, minimum=1, maximum=3600,
           help="seconds the setup probe email may take"),
    Option(name="probe", env="EMI_PROBE", kind="bool", default=True,
           help="send the setup probe email before the first test (on/off)"),
    Option(name="step_timeout", env="EMI_STEP_TIMEOUT", kind="float", default=30.0, minimum=1, maximum=3600,
           help="default seconds per step"),
    Option(name="test_timeout", env="EMI_TEST_TIMEOUT", kind="float", default=90.0, minimum=1, maximum=3600,
           help="default seconds per test"),
    Option(name="job_timeout", env="EMI_JOB_TIMEOUT", kind="float", default=0.0, minimum=0, maximum=86400,
           help="job time budget in seconds (0: P0 300, P1 600, P2-P4 3600, debug 900, all unlimited)"),
    Option(name="run_id", env="EMI_RUN_ID", kind="str", default="",
           help="run ID shared by every job of a run (default: a new one)"),
    Option(name="reports_dir", env="EMI_REPORTS_DIR", kind="str", default="reports", help="report directory"),
    Option(name="log_dir", env="EMI_LOG_DIR", kind="str", default="logs", help="test broker log directory"),
)
OPTIONS_BY_NAME: dict[str, Option] = {option.name: option for option in OPTIONS}


@dataclass(frozen=True)
class Resolved:
    """Every option's value and where it came from.

    Attributes:
        values: name -> typed value.
        sources: name -> source.
    """

    values: dict[str, Any]
    sources: dict[str, Source]

    def __getitem__(self, name: str) -> Any:
        """The value of one option."""
        return self.values[name]

    def describe(self) -> dict[str, dict[str, Any]]:
        """Every option with its source; secrets redacted.

        Returns:
            name -> ``{"value", "source", "env"}``.
        """
        shown = {}
        for option in OPTIONS:
            value = self.values[option.name]
            if option.secret and self.sources[option.name] is not Source.DEFAULT:
                value = REDACTED
            shown[option.name] = {"value": value, "source": str(self.sources[option.name]), "env": option.env}
        return shown

    def emi_settings(self) -> EmiSettings:
        """The API settings these options describe.

        Returns:
            Settings for :class:`emi_api.EmiApi`.

        Raises:
            ValueError: If a combination is invalid.
        """
        folders = tuple(item.strip() for item in self["imap_folders"].split(",") if item.strip()) or ("INBOX",)
        imap = ImapSettings(host=self["imap_host"], port=self["imap_port"],
                            security=ImapSecurity(self["imap_security"]), username=self["imap_user"],
                            password=self["imap_password"], folders=folders,
                            connect_timeout=self["connect_timeout"], read_timeout=self["read_timeout"])
        smtp = SenderSettings(transport=Transport(self["sender_transport"]), host=self["smtp_host"],
                              smtp_port=self["smtp_port"], imap_port=self["imap_port"],
                              security=SmtpSecurity(self["smtp_security"]), username=self["smtp_user"],
                              password=self["smtp_password"],
                              smtp_login=self.sources["smtp_password"] is not Source.DEFAULT,
                              base_mailbox=self["base_mailbox"], connect_timeout=self["connect_timeout"],
                              read_timeout=self["read_timeout"])
        broker = BrokerSettings(port=self["test_broker_port"], imap=imap, smtp=smtp,
                                max_connections=self["max_connections"], log_dir=Path(self["log_dir"]))
        greenmail = GreenmailSettings(host=self["greenmail_host"] or imap.host, api_port=self["greenmail_api_port"],
                                      probe_ports={"smtp": smtp.effective_smtp_port, "imap": imap.effective_port})
        return EmiSettings(mode=ClientMode(self["client_mode"]), broker=broker, greenmail=greenmail,
                           wait_timeout=self["wait_timeout"])


def resolve(
    cli: Mapping[str, Any] | None = None,
    environ: Mapping[str, str] | None = None,
    dotenv: Mapping[str, str | None] | None = None,
    arguments: Mapping[str, Any] | None = None,
) -> Resolved:
    """Resolve every option by precedence.

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
    cli, environ, dotenv, arguments = cli or {}, environ or {}, dotenv or {}, arguments or {}
    unknown = set(arguments) - set(OPTIONS_BY_NAME)
    if unknown:
        raise ValueError(f"unknown option(s): {', '.join(sorted(unknown))}")
    values: dict[str, Any] = {}
    sources: dict[str, Source] = {}
    for option in OPTIONS:
        candidates = ((Source.ARGUMENT, arguments.get(option.name)),
                      (Source.CLI, None if option.secret else cli.get(option.name)),
                      (Source.ENV, environ.get(option.env)), (Source.DOTENV, dotenv.get(option.env)))
        for source, raw in candidates:
            if raw is not None and (source is Source.ARGUMENT or str(raw).strip() != ""):
                values[option.name] = option.convert(raw)
                sources[option.name] = source
                break
        else:
            values[option.name] = option.default
            sources[option.name] = Source.DEFAULT
    return Resolved(values, sources)


def read_dotenv(path: Path = Path(".env")) -> dict[str, str | None]:
    """Read ``.env`` without changing the process environment (a real variable must keep precedence).

    Args:
        path: The file.

    Returns:
        Its values; empty when the file does not exist.
    """
    return dict(dotenv_values(path)) if path.is_file() else {}


def add_arguments(parser: argparse.ArgumentParser | argparse._ArgumentGroup) -> None:
    """Add every non-secret option to an argparse parser (harness CLI).

    Args:
        parser: Parser or argument group.
    """
    for option in OPTIONS:
        if not option.secret:
            parser.add_argument(option.flag, dest=option.dest, default=None, metavar=option.env,
                                help=f"{option.help} [env {option.env}, default {option.default!r}]")


def add_pytest_options(add: Callable[..., Any]) -> None:
    """Add every non-secret option to pytest (``parser.getgroup("emi").addoption``).

    Args:
        add: The group's ``addoption``.
    """
    for option in OPTIONS:
        if not option.secret:
            add(option.flag, dest=option.dest, default=None, metavar=option.env,
                help=f"{option.help} [env {option.env}, default {option.default!r}]")


def cli_values(namespace: Any) -> dict[str, Any]:
    """Option values given on a command line (argparse namespace or pytest ``config.option``).

    Args:
        namespace: Object with ``emi_<name>`` attributes.

    Returns:
        name -> value, for options that were given.
    """
    found = {}
    for option in OPTIONS:
        value = getattr(namespace, option.dest, None)
        if value is not None:
            found[option.name] = value
    return found


def pass_through(namespace: Any) -> list[str]:
    """Rebuild the ``--emi-*`` flags given to the harness CLI for pytest, unchanged.

    Args:
        namespace: The CLI's parsed arguments.

    Returns:
        Flags and values.
    """
    flags: list[str] = []
    for name, value in cli_values(namespace).items():
        flags.extend((OPTIONS_BY_NAME[name].flag, str(value)))
    return flags

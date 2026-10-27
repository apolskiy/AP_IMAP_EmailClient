# SPDX-FileCopyrightText: 2026 Aleksandr Polskiy
# SPDX-License-Identifier: Apache-2.0
"""The option core every tool's settings are built on (design §3, AMB-81).

Each option is defined once: name, ``EMI_*`` variable, type, default, range, help text and secret flag.
Each tool declares its own rows next to its settings; a larger table merges the rows of the tools it uses
(:func:`merge`). Every command line is generated from a table: a tool's own CLI uses the bare flag
(``--smtp-host``), the harness CLI and pytest the ``emi-`` prefix (``--emi-smtp-host``). Precedence,
highest first: **argument in code > CLI option > real environment variable > ``.env`` file > built-in
default**. Secret options (passwords) have no CLI flag and are redacted whenever settings are shown;
every value is reported together with its source.

This package imports no other EMI package, so every tool can use it and still run on its own.
"""

import argparse
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from dotenv import dotenv_values

REDACTED = "***"
SECURITY_CHOICES = ("plain", "ssl", "starttls")


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
        name: Python name (``imap_host``); the CLI flag is ``--imap-host`` (``--emi-imap-host`` prefixed).
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

    def flag(self, prefix: str = "") -> str:
        """The command-line flag.

        Args:
            prefix: Flag prefix such as ``emi-`` (the harness CLI and pytest).

        Returns:
            ``--<prefix><name with dashes>``.
        """
        return f"--{prefix}{self.name.replace('_', '-')}"

    def dest(self, prefix: str = "") -> str:
        """The argparse / pytest destination.

        Args:
            prefix: Flag prefix such as ``emi-``.

        Returns:
            ``<prefix with underscores><name>``.
        """
        return prefix.replace("-", "_") + self.name

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
                raise ValueError(f"{self.flag()} / {self.env} must be {self.minimum:g}-{self.maximum:g}, "
                                 f"got {raw!r}")
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
        raise ValueError(f"{option.flag()} / {option.env} must be a whole number, got {raw!r}") from error


def _decimal(option: Option, raw: Any) -> float:
    """Number option."""
    try:
        return float(str(raw).strip())
    except ValueError as error:
        raise ValueError(f"{option.flag()} / {option.env} must be a number, got {raw!r}") from error


def _boolean(option: Option, raw: Any) -> bool:
    """On/off option."""
    if isinstance(raw, bool):
        return raw
    text = str(raw).strip().lower()
    if text in ("1", "true", "yes", "on"):
        return True
    if text in ("0", "false", "no", "off"):
        return False
    raise ValueError(f"{option.flag()} / {option.env} must be on/off, got {raw!r}")


def _choice(option: Option, raw: Any) -> str:
    """One of a fixed set."""
    text = str(raw).strip().lower()
    if text not in option.choices:
        raise ValueError(f"{option.flag()} / {option.env} must be one of {', '.join(option.choices)}, "
                         f"got {raw!r}")
    return text


_CONVERTERS: dict[str, Callable[[Option, Any], Any]] = {
    "str": _text, "int": _integer, "float": _decimal, "bool": _boolean, "choice": _choice}


def merge(*tables: Iterable[Option]) -> tuple[Option, ...]:
    """Join tool tables into one, in order; a name declared twice must be the identical row.

    Args:
        *tables: Option rows of each tool.

    Returns:
        Every option once.

    Raises:
        ValueError: If two tools declare the same name differently.
    """
    joined: dict[str, Option] = {}
    for table in tables:
        for option in table:
            known = joined.setdefault(option.name, option)
            if known != option:
                raise ValueError(f"option {option.name!r} is declared twice with different definitions")
    return tuple(joined.values())


@dataclass(frozen=True)
class Resolved:
    """Every option's value and where it came from.

    Attributes:
        values: name -> typed value.
        sources: name -> source.
        options: The table the values were resolved from.
    """

    values: dict[str, Any]
    sources: dict[str, Source]
    options: tuple[Option, ...] = ()

    def __getitem__(self, name: str) -> Any:
        """The value of one option."""
        return self.values[name]

    def given(self, name: str) -> bool:
        """Whether an option was set anywhere rather than left at its default.

        Args:
            name: Option name.

        Returns:
            True unless the value is the built-in default.
        """
        return self.sources[name] is not Source.DEFAULT

    def describe(self) -> dict[str, dict[str, Any]]:
        """Every option with its source; secrets redacted.

        Returns:
            name -> ``{"value", "source", "env"}``.
        """
        shown = {}
        for option in self.options:
            value = self.values[option.name]
            if option.secret and self.given(option.name):
                value = REDACTED
            shown[option.name] = {"value": value, "source": str(self.sources[option.name]), "env": option.env}
        return shown


def resolve(  # pylint: disable=too-many-arguments
    options: tuple[Option, ...],
    cli: Mapping[str, Any] | None = None,
    environ: Mapping[str, str] | None = None,
    dotenv: Mapping[str, str | None] | None = None,
    arguments: Mapping[str, Any] | None = None,
) -> Resolved:
    """Resolve every option of a table by precedence.

    Args:
        options: The table.
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
    unknown = set(arguments) - {option.name for option in options}
    if unknown:
        raise ValueError(f"unknown option(s): {', '.join(sorted(unknown))}")
    values: dict[str, Any] = {}
    sources: dict[str, Source] = {}
    for option in options:
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
    return Resolved(values, sources, options)


def read_dotenv(path: Path = Path(".env")) -> dict[str, str | None]:
    """Read ``.env`` without changing the process environment (a real variable must keep precedence).

    Args:
        path: The file.

    Returns:
        Its values; empty when the file does not exist.
    """
    return dict(dotenv_values(path)) if path.is_file() else {}


def add_arguments(add: Callable[..., Any], options: tuple[Option, ...], prefix: str = "") -> None:
    """Add every non-secret option of a table to a command line.

    Args:
        add: ``parser.add_argument``, an argument group's, or pytest's ``addoption``.
        options: The table.
        prefix: Flag prefix (``emi-`` for the harness CLI and pytest).
    """
    for option in options:
        if not option.secret:
            add(option.flag(prefix), dest=option.dest(prefix), default=None, metavar=option.env,
                help=f"{option.help} [env {option.env}, default {option.default!r}]")


def cli_values(namespace: Any, options: tuple[Option, ...], prefix: str = "") -> dict[str, Any]:
    """Option values given on a command line (argparse namespace or pytest ``config.option``).

    Args:
        namespace: Object with the options' destinations as attributes.
        options: The table.
        prefix: Flag prefix the options were added with.

    Returns:
        name -> value, for options that were given.
    """
    found = {}
    for option in options:
        value = getattr(namespace, option.dest(prefix), None)
        if value is not None:
            found[option.name] = value
    return found


def from_command_line(args: argparse.Namespace, options: tuple[Option, ...], environ: Mapping[str, str],
                      dotenv_path: Path = Path(".env")) -> Resolved:
    """Resolve a tool CLI's settings: its flags, then the environment, then ``.env``, then defaults.

    Args:
        args: The parsed command line (flags added by :func:`add_arguments` without a prefix).
        options: The tool's table.
        environ: Real environment variables.
        dotenv_path: The ``.env`` file.

    Returns:
        Values and sources.

    Raises:
        ValueError: If a value has the wrong type or is out of range.
    """
    return resolve(options, cli_values(args, options), environ, read_dotenv(dotenv_path))

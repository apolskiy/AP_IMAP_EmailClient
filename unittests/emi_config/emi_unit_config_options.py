# SPDX-FileCopyrightText: 2026 Aleksandr Polskiy
# SPDX-License-Identifier: Apache-2.0
"""Unit tests: one option table per tool, merged upward, every CLI generated from it (design §3, AMB-81)."""

import argparse
from pathlib import Path

import pytest

from emi_api.settings import OPTIONS as API_OPTIONS
from emi_config import Option, Source, from_command_line, merge, resolve
from emi_greenmail.settings import OPTIONS as GREENMAIL_OPTIONS
from emi_harness.options import OPTIONS as HARNESS_OPTIONS
from emi_reader.settings import OPTIONS as READER_OPTIONS
from emi_sender.cli import build_parser as sender_parser
from emi_sender.settings import OPTIONS as SENDER_OPTIONS
from emi_sender.settings import sender_settings_from
from emi_test_broker.cli import build_parser as broker_parser
from emi_test_broker.settings import OPTIONS as BROKER_OPTIONS

_ROW = Option(name="sample_port", env="EMI_SAMPLE_PORT", kind="int", default=1, minimum=1, maximum=9, help="x")


def _flags(parser: argparse.ArgumentParser) -> set[str]:
    """Every flag a parser accepts."""
    return {flag for action in parser._actions for flag in action.option_strings}  # pylint: disable=protected-access


class TestEMIConfigOptions:
    """The option core and the tables built on it."""

    def EMI_CONFIG_UNIT_140001_merge_joins_identical_rows_only(self) -> None:
        """A row two tools declare identically appears once; two different definitions are refused."""
        assert merge((_ROW,), (_ROW,)) == (_ROW,)
        other = Option(name="sample_port", env="EMI_SAMPLE_PORT", kind="int", default=2, help="x")
        with pytest.raises(ValueError, match="declared twice"):
            merge((_ROW,), (other,))

    def EMI_CONFIG_UNIT_140002_flags_with_and_without_prefix(self) -> None:
        """One name everywhere: ``--sample-port`` in a tool's CLI, ``--emi-sample-port`` under the harness."""
        assert (_ROW.flag(), _ROW.dest()) == ("--sample-port", "sample_port")
        assert (_ROW.flag("emi-"), _ROW.dest("emi-")) == ("--emi-sample-port", "emi_sample_port")
        with pytest.raises(ValueError, match="--sample-port / EMI_SAMPLE_PORT must be 1-9"):
            resolve((_ROW,), environ={"EMI_SAMPLE_PORT": "10"})

    def EMI_CONFIG_UNIT_140003_tool_clis_generated_from_their_tables(self) -> None:
        """The sender and broker CLIs accept every non-secret row of their table, and no secret one."""
        for parser, table in ((sender_parser(), SENDER_OPTIONS), (broker_parser(), BROKER_OPTIONS)):
            flags = _flags(parser)
            assert {option.flag() for option in table if not option.secret} <= flags
            assert not {option.flag() for option in table if option.secret} & flags
        assert not {"--host", "--security", "--transport", "--port"} & (_flags(sender_parser()) |
                                                                        _flags(broker_parser()))

    def EMI_CONFIG_UNIT_140004_tables_nest_unchanged(self) -> None:
        """Each larger table holds every row of the tables it uses, unchanged: reader and sender in the
        broker, the broker and Greenmail in the API, the API in the harness."""
        for small, large in ((READER_OPTIONS, BROKER_OPTIONS), (SENDER_OPTIONS, BROKER_OPTIONS),
                             (BROKER_OPTIONS, API_OPTIONS), (GREENMAIL_OPTIONS, API_OPTIONS),
                             (API_OPTIONS, HARNESS_OPTIONS)):
            assert set(small) <= set(large)
        assert len({option.name for option in HARNESS_OPTIONS}) == len(HARNESS_OPTIONS)

    def EMI_CONFIG_UNIT_140005_tool_cli_precedence(self, tmp_path: Path) -> None:
        """A tool CLI resolves flag > real environment > ``.env`` > default, each with its source; APPEND
        logs in with the IMAP password."""
        dotenv = tmp_path / ".env"
        dotenv.write_text("EMI_SMTP_HOST=dotenv.host\nEMI_SMTP_USER=dotenv.user\nEMI_IMAP_PASSWORD=imap-pw\n",
                          encoding="utf-8")
        args = sender_parser().parse_args(["--smtp-port", "2525", "--sender-transport", "append"])
        resolved = from_command_line(args, SENDER_OPTIONS, {"EMI_SMTP_HOST": "env.host"}, dotenv)
        assert (resolved["smtp_port"], resolved.sources["smtp_port"]) == (2525, Source.CLI)
        assert (resolved["smtp_host"], resolved.sources["smtp_host"]) == ("env.host", Source.ENV)
        assert (resolved["smtp_user"], resolved.sources["smtp_user"]) == ("dotenv.user", Source.DOTENV)
        assert resolved.sources["base_mailbox"] is Source.DEFAULT
        settings = sender_settings_from(resolved)
        assert settings.password == "imap-pw" and not settings.smtp_login

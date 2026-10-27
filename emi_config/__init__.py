# SPDX-FileCopyrightText: 2026 Aleksandr Polskiy
# SPDX-License-Identifier: Apache-2.0
"""EMI option core: one option table per tool, merged for larger tools (design §3, AMB-81)."""

from emi_config.options import (
    REDACTED,
    SECURITY_CHOICES,
    Option,
    Resolved,
    Source,
    add_arguments,
    cli_values,
    from_command_line,
    merge,
    read_dotenv,
    resolve,
)

__all__ = [
    "REDACTED",
    "SECURITY_CHOICES",
    "Option",
    "Resolved",
    "Source",
    "add_arguments",
    "cli_values",
    "from_command_line",
    "merge",
    "read_dotenv",
    "resolve",
]

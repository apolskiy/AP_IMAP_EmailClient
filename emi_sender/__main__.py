# SPDX-FileCopyrightText: 2026 Aleksandr Polskiy
# SPDX-License-Identifier: Apache-2.0
"""Entry point for ``python -m emi_sender``."""

import sys

from emi_sender.cli import main

sys.exit(main())

# SPDX-FileCopyrightText: 2026 Aleksandr Polskiy
# SPDX-License-Identifier: Apache-2.0
"""Registers the EMI harness plugin for every pytest run in the repository (design §9).

``pytester`` lets the harness unit tests run small inner pytest sessions with the plugin.
"""

pytest_plugins = ["emi_harness.plugin", "pytester"]

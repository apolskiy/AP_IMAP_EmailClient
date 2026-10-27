# SPDX-FileCopyrightText: 2026 Aleksandr Polskiy
# SPDX-License-Identifier: Apache-2.0
"""Network fault layer 3 (design §13.2, AMB-77): real Greenmail outages, marker ``docker``.

Left out of the default unit run (``-m "not docker"``); ``python -m pytest unittests/outage -m docker``
selects them. They stop and restart the Greenmail container, so never run them next to a functional run.
"""

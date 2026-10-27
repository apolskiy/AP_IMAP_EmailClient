# SPDX-FileCopyrightText: 2026 Aleksandr Polskiy
# SPDX-License-Identifier: Apache-2.0
"""The outage tests' container fixture: skipped without Docker, Greenmail always running afterwards."""

from collections.abc import Iterator

import pytest

from emi_harness.keywords import HarnessKeyword
from unittests.outage.greenmail_container import GreenmailContainer


@pytest.fixture(name="greenmail")
def _greenmail() -> Iterator[GreenmailContainer]:
    """The Greenmail container; skipped with ``HARNESS_DOCKER_UNAVAILABLE`` when it cannot be controlled.

    Whatever the test did, the container is running and Greenmail ready when it ends.
    """
    container = GreenmailContainer()
    reason = container.unavailable_reason()
    if reason:
        pytest.skip(f"{HarnessKeyword.DOCKER_UNAVAILABLE}: {reason}")
    try:
        yield container
    finally:
        if not container.running():
            container.start()

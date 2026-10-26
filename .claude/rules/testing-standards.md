<!--
SPDX-FileCopyrightText: 2026 Aleksandr Polskiy
SPDX-License-Identifier: Apache-2.0
-->
# Testing Standards & Naming Rules

## Test Naming Convention
* **Project Prefix**: `EMI` is mandatory on all test artifacts.
* **Test Module**: `emi_<layer>_<component>.py`
* **Test Class**: `TestEMI<Component>`
* **Test Callable**: `EMI_<MODULE>_<LAYER>_<ID>_<behavior>` (e.g., `EMI_TOOL_100001_waits_for_activation_email`)
* **TestRail ID**: Unique numbers of **at least 6 digits** (`\d{6,}`) in the test name.
* **ASCII only**: Test IDs, run IDs and every identifier that ends up in an email address, header or marker line are restricted to ASCII `[A-Za-z0-9_-]`; anything else (e.g. Cyrillic) is rejected.
* **Test Scale**: Partitioned test suites must encompass no less than **1,000 tests** across parametrized fixtures.
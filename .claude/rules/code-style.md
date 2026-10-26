<!--
SPDX-FileCopyrightText: 2026 Aleksandr Polskiy
SPDX-License-Identifier: Apache-2.0
-->
# Project Code Style

Conventions every Python file in this repository must follow.

## 1. Naming & Variable Standards
* **No Single-Character Variables**: Single-letter variable names (`i`, `e`, `x`, `k`, `v`, `f`) are **strictly prohibited** in all scopes (including loops, list comprehensions, lambda functions, and exception handlers).
* **Minimum Length & Clarity**: All variable names MUST be at least **3 characters long** and clearly articulate their purpose (e.g., `index` instead of `i`, `error` or `exception` instead of `e`, `item_value` instead of `x`).
* **One exception, and it is narrow**: `id` is permitted as an **attribute** name where the record mirrors a third-party wire contract, because a double that renames a provider's field stops proving that our adapter reads the real one. It stays prohibited for variables, arguments and inline bindings, where the name is ours to choose and would shadow the builtin.
* **Module-Level Constants**: Private module constants use `_UPPER_SNAKE_CASE` with a leading underscore (`_CONFIG_PATH`, `_DEFAULT_TIMEOUT_SEC`).
* **Encapsulated State**: Instance attributes are private (`self._client`, `self._retry_limit`). Expose attributes through `@property` decorators.
* **Private Helpers**: Private functions take a leading underscore (`_parse_json_payload`).
* **Class & Module Naming**: Classes use `PascalCase`; modules use `snake_case`.

## 1.1 SPDX Authorship Header
Every Python file opens with:
```python
# SPDX-FileCopyrightText: 2026 Aleksandr Polskiy
# SPDX-License-Identifier: Apache-2.0
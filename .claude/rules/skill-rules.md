<!--
SPDX-FileCopyrightText: 2026 Aleksandr Polskiy
SPDX-License-Identifier: Apache-2.0
-->
# AI Skill & Process Governance

* **Audit & Logging**: `CLAUDE_LOG.md` tracks architectural decisions and Pylint scores. `.claude/logs/PROMPT_LOG.md` stores raw assistant prompts (ignored by git).
* **Lifecycle Phases**:
  * **Phase 0**: Ambiguity resolution & register creation (`docs/design/phase0_*_ambiguities.md`).
  * **Phase 1**: Discussion & API contract clarification.
  * **Phase 2**: Design document creation (`docs/design/*.md`).
  * **Phase 3**: Coding implementation (Pylint 10/10, type hints, no 1-char variables).
  * **Phase 4**: Testing & multi-tier validation.
  * **Iterative Gaps**: Continuous gap analysis, edge-case hardening, and targeted refactoring.
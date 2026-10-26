<!--
SPDX-FileCopyrightText: 2026 Aleksandr Polskiy
SPDX-License-Identifier: Apache-2.0
-->
# `.claude/`

Configuration for the AI collaborator (Claude Code) used to build this project. The top-level
instructions are in [`CLAUDE.md`](../CLAUDE.md); the rules below are loaded with them in every session.

| File | What it governs |
|---|---|
| [`rules/claude_md_shell_rules.md`](rules/claude_md_shell_rules.md) | Shell use on Windows: no heredocs, no bare `python -`, programs run from script files |
| [`rules/code-style.md`](rules/code-style.md) | Naming (no names under 3 characters), private state, SPDX header |
| [`rules/framework-rules.md`](rules/framework-rules.md) | CI gates, priorities P0-P4, standalone unit jobs, overlapping runs |
| [`rules/skill-rules.md`](rules/skill-rules.md) | Lifecycle phases 0-4 and audit logging |
| [`rules/testing-standards.md`](rules/testing-standards.md) | Test naming, TestRail IDs, ASCII-only identifiers, test scale |
| [`rules/worktree-rules.md`](rules/worktree-rules.md) | No AI commits, secrets protection, IDE ignores |

<!--
SPDX-FileCopyrightText: 2026 Aleksandr Polskiy
SPDX-License-Identifier: Apache-2.0
-->
# Git Worktree & Environment Safety

* **No Direct Commits**: The AI assistant never executes `git commit` or `git push`.
* **Secrets Protection**: API keys, `.env`, and local credentials are never logged or committed.
* **PyCharm & IDE Ignores**: `.idea/` and `.venv/` must be ignored in `.gitignore`.
---
description: The 1Password account flag requirement on shell calls and proposed writes.
status: stable
tags: [alpha]
title: Guard op
type: Reference
---

# Guard op

`guard-op` requires the configured `[op] account` flag on shell calls and proposed writes in every directory.
`PreToolUse` covers Claude `Bash` and `Write|Edit|MultiEdit|NotebookEdit`, Codex `Bash`/`shell`/`apply_patch` and Antigravity `run_command`/edit tools.
The guard scans shell files, Markdown shell fences, Python command expressions, notebook code cells, and YAML command fields.
It allows unrelated edits to existing offenders.
`OP_ACCOUNT` alone doesn't pass the guard.
The module docstring lists the allowed command forms that the scanner misses.
Check the Codex `apply_patch` PreToolUse matcher live after install.
Its behavior remains unverified against Codex's documentation.

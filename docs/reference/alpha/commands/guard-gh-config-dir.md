---
description: The hook that blocks gh commands when GH_CONFIG_DIR is unset or empty.
generated:
  - role: generated
    harness: claude-code:2.1.319
    model: anthropic:claude-sonnet-5-5
    effort: high
    surface: sdk-cli
    session: sha-256-64:e23dce32b8825437
    at: "2026-11-26T03:16:54Z"
status: stable
tags: [alpha]
title: Guard gh config dir
type: Reference
---

# Guard gh config dir

`guard-gh-config-dir` is a PreToolUse hook that blocks `gh` commands when `GH_CONFIG_DIR` is unset or empty in the hook's environment.
It scans the command string without running `gh` and accepts any nonempty value.

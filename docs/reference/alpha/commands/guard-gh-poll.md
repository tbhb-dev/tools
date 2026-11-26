---
description: The hook that blocks GitHub polling, force pushes and git hook bypasses.
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
title: Guard GitHub polling
type: Reference
---

# Guard GitHub polling

`guard-gh-poll` is a PreToolUse hook that blocks GitHub polling and unsetting the 1Password service account token's variable.
It allows `gh run list --limit 1` and a list endpoint with `per_page=1`.

`guard-gh-poll` also denies git hook bypasses: `git -c core.hooksPath`, writing `core.hooksPath` and `--no-verify`.
Reads of the key pass.

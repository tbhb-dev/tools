---
description: The hook that blocks GitHub polling, force pushes and git hook bypasses.
status: stable
tags: [alpha]
title: Guard gh poll
type: Reference
---

# Guard gh poll

`guard-gh-poll` is a PreToolUse hook that blocks GitHub polling and unsetting the 1Password service account token's variable.
It allows `gh run list --limit 1` and a list endpoint with `per_page=1`.

`guard-gh-poll` also denies git hook bypasses: `git -c core.hooksPath`, writing `core.hooksPath` and `--no-verify`.
Reads of the key pass.

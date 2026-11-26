---
description: The SessionStart hook that reports the host, its role, and the top commands.
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
title: Session context
type: Reference
---

# Session context

`session-context` is a Claude Code SessionStart hook that reports the host, its role, the other hosts, the local time, and a reminder to use `tbhbdev alpha` for GitHub writes, with the top commands (`pr create|check`, `pr ready`, `pr close`, `merge`, `review draft`, `agent exec`) and their arguments.
See [Review drafts](../review-drafts.md) for the review commands that write to GitHub.

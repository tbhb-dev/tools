---
description: The Claude Code hook that blocks sign-off and co-author trailers and open-note items without decisions.
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
title: State guard
type: Reference
---

# State guard

`state-guard` is a Claude Code hook that blocks a `git commit` that adds a sign-off or co-author trailer, and a new open-note item without a `Decisions checked:` line.
It doesn't judge status claims in replies or messages.

---
description: The GitHub command-line tool wrapper on PATH that budgets agent sessions' calls.
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
title: GitHub command-line tool shim
type: Reference
---

# GitHub command-line tool shim

`gh-shim` is the `gh` that agent sessions find first on PATH.
It budgets their `gh` calls as a backstop.

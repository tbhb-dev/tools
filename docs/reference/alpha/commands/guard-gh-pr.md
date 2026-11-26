---
description: The hook that blocks raw GitHub command-line tool writes and names the tbhbdev alpha command to run instead.
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
title: Guard GitHub pull request writes
type: Reference
---

# Guard GitHub pull request writes

`guard-gh-pr` is a PreToolUse hook that blocks `gh pr create`, `gh pr edit`, `gh pr merge`, `gh pr ready`, `gh pr close`, `gh issue close`, `gh run rerun` and a `gh api` DELETE of a branch ref, starts its denial with `tbhbdev hint:`, names the `tbhbdev alpha` command to run instead, and ends with a pointer to `tbhbdev alpha --help`.

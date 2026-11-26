---
description: Peer-run brief skeletons and checks.
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
title: Brief
type: Reference
---

# Brief

`tbhbdev alpha brief new RUN --repo OWNER/REPO` writes a peer-run brief skeleton to stdout, or to a file with `-o FILE`.
Fill its placeholders, then run `tbhbdev alpha brief check FILE...` to check the completed brief.
Use `--branch` and `--base` to override the worktree defaults.
`--push --scan-command CMD` includes the required outgoing scan command supplied by the operator.
The command must be non-empty and contain no newline or backtick.
The check normalizes Unicode, whitespace and case before searching for forbidden instructions and removing fixed sentences in their generated sections.
It matches phrases by spelling and section placement.
Authors can add lines in Hard rules.
Use it to catch omissions and common mistakes.
Review the brief's intent separately.

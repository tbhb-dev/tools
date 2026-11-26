---
description: The Stop hook that asks unfinished builds and reviews to continue in agent exec runs.
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
title: Run stop hook
type: Reference
---

# Run stop hook

`run-stop-hook <claude|codex|antigravity>` is a Stop hook for `agent exec`. It asks unfinished builds and reviews to continue. After three blocks it allows the run to stop and records the reason in the run log. It exits immediately in interactive sessions.

---
description: Agent and model attribution, the Generated-by trailers, and the identity start time.
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
title: Identity
type: Reference
---

# Identity

`identity` records which agent and model made each edit and writes the `Generated-by` trailers.

## Identity start time

An identity line can record when its agent started and which launch it belongs to.
Both attributes are optional.
A line without them is still valid.

| Attribute | Value |
| --- | --- |
| `launch` | The `sha-256-64:` hash of the launcher's run id. |
| `started_at` | The agent's start time in Coordinated Universal Time with whole seconds, as `YYYY-MM-DDTHH:MM:SSZ`. The `T` and `Z` are capitals. |

Order is alphabetical:

```text
Generated-by: LLM effort=high;harness=codex:0.164.0;launch=sha-256-64:0a1b2c3d4e5f6071;model=openai:gpt-6.1-sol;session=sha-256-64:fb2bbc10b42a2d56;started_at=2026-11-14T00:02:41Z;surface=exec
```

The `record` hooks set both fields once in the session record, and a resumed session keeps its first values.

- A launcher writes `tbhb-alpha/launch.json` in the worktree's git directory, with a `launch` hash and a `started_at` time, before the harness starts.
  The first root session to run a hook there claims the file by adding its own hash as `session`, then copies both fields into its record.
  A later session finds the file claimed and inherits nothing from it.
- A session with no launch file to claim takes `started_at` from its first hook event and omits `launch`.
- A subagent takes `started_at` from the first hook event with its agent id, and inherits `launch` from its root session.

The `trailer` hook writes both attributes from the session record.
Where lines for the same agent meet, on an amend, a rebase, or an interactive squash, the hook merges them.
Lines with the same key and every attribute except `started_at` become one line, the one with the earliest `started_at`.
Lines that differ in another attribute, including `effort`, remain distinct.
A line with the attribute replaces one without it.

The `check` hook and `verify-range` accept both attributes.
They require a real Coordinated Universal Time in the exact format for `started_at` and the hash format for `launch`.
An agent commit whose `Generated-by` line lacks `started_at` passes with a warning.

The rules `commit_identity` check must accept both attributes before a host installs these hooks.
Until it does, commits with them fail CI.

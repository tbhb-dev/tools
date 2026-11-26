---
description: The hook that requires an explicit subagent model and checks the model of a fork.
generated:
  - role: generated
    harness: codex:0.171.0
    model: openai:gpt-6.1-sol
    effort: high
    surface: exec
    session: sha-256-64:a2c94d584e371b6e
    at: "2026-11-26T05:26:48Z"
status: stable
tags: [alpha]
title: Guard agent model
type: Reference
---

# Guard agent model

`guard-agent-model` is a PreToolUse hook for Claude Code's `Agent` tool. It refuses a launch, with exit 2 and the reason on stderr, when `tool_input.model` is missing or empty. A subagent without `model` inherits the session's model, and a fork always does, so a session on a costly model would launch costly subagents by accident. A fork has no `model` to check, so the guard reads the session's model from the last 400 kB of the payload's `transcript_path`: the `message.model` of the last line that has one, skipping values that start with `<`, such as `<synthetic>`. It allows the fork when that model's id has neither `opus` nor `fable`. It refuses the fork, naming the model, when the id has either, and refuses it as `unknown` when the path is missing or the file is unreadable or holds no model. Use `sonnet` for most work, and `opus` or `fable` only for planning and design. Any other tool, and input the hook can't read, passes. `alpha install` registers it for Claude Code with matcher `Agent`.

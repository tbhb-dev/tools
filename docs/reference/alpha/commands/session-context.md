---
description: The SessionStart hook that reports the host, its role, and the top commands.
status: stable
tags: [alpha]
title: Session context
type: Reference
---

# Session context

`session-context` is a Claude Code SessionStart hook that reports the host, its role, the other hosts, the local time, and a reminder to use `tbhbdev alpha` for GitHub writes, with the top commands (`pr create|check`, `pr ready`, `pr close`, `merge`, `review draft`, `agent exec`) and their arguments.
See [Review drafts](../review-drafts.md) for the review commands that write to GitHub.

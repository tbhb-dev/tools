---
description: Draft architecture decision records from GitHub Decision issues.
generated:
  - role: generated
    harness: codex:0.171.0
    model: openai:gpt-6.2-sol
    at: "2026-11-24T19:58:00Z"
status: stable
tags: [decisions, records]
title: Decision record commands
type: Reference
---

# Decision record commands

<!-- cspell:words adrs -->

`tbhbdev adr new --from-decision N` reads issue `N` from the current clone's GitHub origin. Use `--repo OWNER/REPO` to read from another repository. The issue must be an open Decision that proposes an architecture decision record and has no link to the record's pull request.

```sh
tbhbdev adr new --from-decision 42
tbhbdev adr new --from-decision 42 --repo OWNER/REPO --dir adrs --supersedes 0007
```

The command writes `adrs/NNNN-<slug>.md` relative to the current directory and prints the path. Use `--dir DIR` to change the directory. The draft starts with the issue's title, question, recommended option, and undo text. Review the wording and finish the record before opening its pull request. The command prints any findings from the record body check to standard error.

With `--supersedes NNNN`, the draft links to the replaced record. The command also marks that record deprecated and adds the new record's number to its frontmatter.

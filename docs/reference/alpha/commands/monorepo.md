---
description: Mapping a public repository's files and importing them into the monorepo.
generated:
  - role: generated
    harness: codex:0.171.0
    model: openai:gpt-6.1-sol
    effort: high
    surface: exec
    session: sha-256-64:2c8622ec6366c578
    at: "2026-11-26T05:42:38Z"
status: stable
tags: [alpha]
title: Monorepo commands
type: Reference
---

# Monorepo commands

`monorepo map CLONE COMMIT --dest FOLDER` writes the path map for importing a public repository into the monorepo, and `monorepo import CLONE@COMMIT --map MAP --into CLONE` makes the import commit. See [Monorepo import](../monorepo.md).

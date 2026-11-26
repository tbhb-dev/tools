---
description: Mapping a public repository's files and importing them into the monorepo.
generated:
  - role: generated
    harness: codex
    model: openai:gpt-6.1-sol
    session: sha-256-64:f5dc77425c10fed5
    at: "2026-11-26T07:28:49Z"
status: stable
tags: [alpha]
title: Monorepo commands
type: Reference
---

# Monorepo commands

`monorepo map CLONE COMMIT --dest FOLDER` writes the path map for importing a public repository into the monorepo. `monorepo import CLONE@COMMIT --map MAP --into CLONE` makes the import commit, and `monorepo verify CLONE@COMMIT --map MAP --tree CLONE` checks it. See [Monorepo import](../monorepo.md).

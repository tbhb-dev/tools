---
description: The library that requests a repository-restricted token for the ops deployer.
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
title: Repository token library
type: Reference
---

# Repository token library

`tbhbdev.alpha.repo_token` is a library for the ops deployer.
It uses explicit Connect credentials to request a token restricted to one repository and run Git with that token in memory.

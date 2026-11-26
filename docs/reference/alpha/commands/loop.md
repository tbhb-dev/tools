---
description: Configuration validation, the shadow runner, and the store.
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
title: Loop
type: Reference
---

# Loop

`loop` validates its user configuration and refuses live intake until adapters connect the runner to its sources.
Its shadow runner accepts typed event and reconcile interfaces for simulation.
The package includes pure state derivation and rules that produce typed shadow intents with action keys and spawn budgets.
A SQLite store records receipts, action keys, runs, and escalation markers.

---
description: Configuration validation, the shadow runner, and the store.
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

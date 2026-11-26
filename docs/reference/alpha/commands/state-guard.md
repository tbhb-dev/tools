---
description: The Claude Code hook that blocks sign-off and co-author trailers and open-note items without decisions.
status: stable
tags: [alpha]
title: State guard
type: Reference
---

# State guard

`state-guard` is a Claude Code hook that blocks a `git commit` that adds a sign-off or co-author trailer, and a new open-note item without a `Decisions checked:` line.
It doesn't judge status claims in replies or messages.

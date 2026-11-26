---
description: The hook that blocks gh commands when GH_CONFIG_DIR is unset or empty.
status: stable
tags: [alpha]
title: Guard gh config dir
type: Reference
---

# Guard gh config dir

`guard-gh-config-dir` is a PreToolUse hook that blocks `gh` commands when `GH_CONFIG_DIR` is unset or empty in the hook's environment.
It scans the command string without running `gh` and accepts any nonempty value.

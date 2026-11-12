---
description: Commands and configuration from installed packages.
generated:
  - role: generated
    harness: codex
    model: openai:gpt-6
    at: "2026-11-12T03:45:29Z"
status: stable
tags: [plugins]
title: Plugins
type: Reference
---

# Plugins

Another installed package can add commands and config through an entry point in the `tbhbdev.plugins` group. The entry point resolves to a module or other object with either or both of:

- `TOOLS: dict[str, Tool]`, extra commands. A name that's already taken is an error, never a silent override.
- `config(name: str) -> str | None`, the text of a named config file, or `None`. The package doesn't include a config file of its own, so there's no default for a plugin's file to replace. The built-in tools read deployment values from the user config alone, never from a plugin.

Plugins load on first use. When one fails to import, `tbhbdev` names it and the error on stderr, and the built-in commands keep working. To run `tbhbdev` with a plugin, install both in one environment:

```sh
uvx --from git+https://github.com/tbhb-dev/tools --with <plugin package> tbhbdev <tool> [args...]
```

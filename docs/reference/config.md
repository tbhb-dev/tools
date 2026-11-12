---
description: The user configuration file and deployment settings.
generated:
  - role: generated
    harness: codex
    model: openai:gpt-6
    at: "2026-11-12T03:35:46Z"
status: stable
tags: [config]
title: Config
type: Reference
---

# Config

The values of one deployment come from the user config at `$XDG_CONFIG_HOME/tbhbdev/config.toml`, where `XDG_CONFIG_HOME` defaults to `.config` in the home directory. The package has no defaults for them, and no environment variable or plugin overrides them. When the file or a required key is missing, the command fails, and its error gives the file's path and the `[table] key`. Optional settings include the `[workspace]`, `[loop]`, `[agent]` and `[gh_budget]` tables and `[gitleaks] session_sockets`. Calling `alpha loop` requires its settings in `[loop]`. `tbhbdev` reads the file only when a command first needs a value, so `--help` works without it.

```toml
[op]
account = "example.1password.com"

[connect]
server = "example-connect"
server_hosts = ["nas-a", "nas-b"]
token_prefix = "example-host"

[agent]
host_tag = "tag:example-agents"

[vaults]
all = "vault-all"
host_prefix = "vault-"

[gitleaks]
owners = ["example-org", "example-user"]
session_sockets = ["/tmp/example-socks/"]

[workspace]
root = "src"
repos = ["example-org/app"]

[gh_budget]
hourly_limit = 300
core_floor = 1000
```

`[gh_budget]` sets the agent `gh` budget that [GitHub polling](alpha/github-polling.md) describes. Its values are whole numbers, and the ones shown are the defaults.

Each `[connect] server_hosts` entry must be a DNS label of at most 63 characters. Letters and digits make up the label, and a hyphen may appear only between them. A bad entry fails with its position in the list, not its value.

That's the operator's file. On each agent host, `tbhbdev host bootstrap` installs a copy that adds `[connect] endpoints`, computed from the tailnet as [Agent hosts](agent-hosts.md) describes.

---
description: Secret storage, access, and item management through 1Password.
generated:
  - role: generated
    harness: codex
    model: openai:gpt-6
    at: "2026-11-12T03:35:46Z"
status: stable
tags: [secrets]
title: Secrets
type: Reference
---

# Secrets

Tools get secrets only from `tbhbdev.secrets`. It's the one module that talks to 1Password Connect or `op` at runtime, so a fallback for hosts off the tailnet can go there later. `secrets.read("vault/item/field")` returns one value, and also takes the `op://vault/item/field` form. `secrets.Store` covers item management: get, list, create, edit, delete, and move.

The library reads the Connect token from the current user's own store: the agent keychain on macOS, and on Linux the file `.config/tbhbdev/secrets/connect-token` under `$HOME` (`secrets.LINUX_TOKEN_FILE`). Linux always uses `$HOME` as the base, so configuration management can write the token without consulting `XDG_CONFIG_HOME`. The token needs a regular file with mode `0600` in a `0700` `secrets` directory. The current user must own both. The reader rejects symlinks and any group or other access. Configuration management is the only writer of the Linux token file. The library tries the config's `[connect] endpoints` in order, the list bootstrap writes into an agent host's copy. A connection error or a 5xx answer moves it to the next endpoint, while a 4xx answer stops it.

Tests set the `TBHBDEV_TEST_TOKEN` environment variable, and the library then uses its value in place of the keychain token. Production code leaves it unset.

`tbhbdev secret read REF` prints exactly one value, for `$(tbhbdev secret read ...)`. `get` prints the item's metadata and field labels, adding values with `--reveal`. `create` and `edit` read the fields as a JSON object of label to value from stdin or `--file`, never from the command line. `delete` asks for `--yes`.

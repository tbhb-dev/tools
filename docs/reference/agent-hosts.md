---
description: Agent host setup, upgrades, access, and configuration.
generated:
  - role: generated
    harness: codex
    model: openai:gpt-6
    at: "2026-11-12T03:35:46Z"
status: stable
tags: [agent-hosts]
title: Agent hosts
type: Reference
---

# Agent hosts

The maintainers froze `tbhbdev host` and are retiring it. On Linux, tbhbdev reads the token file. Configuration management is its only writer. Bootstrap, upgrade, grant, revoke, and token installation refuse Linux hosts before any 1Password work. A Linux host without the file loses token access when this change merges.

`tbhbdev host` runs on the operator's Mac, which has 1Password Desktop and the `op` command-line tool. A tag marks the agent hosts on the tailnet. The config's `[agent] host_tag` names it. Each host gets a token for its own vault, `[vaults] host_prefix` followed by the host's name, and for `[vaults] all`, plus any the operator grants, stored in the `agent` account's keychain on that host. Every `op` call names the config's `[op] account`.

- `list` prints each agent host on the tailnet with its OS, and marks this machine.
- `bootstrap [--host H ... | --all] [--repair-acl] [VAULT ...]` does a host's one-time, privileged setup, then runs `upgrade` on the same hosts. It sets up the Connect server and its vaults, the `devel` group and the `agent` user, workspace access, the agent keychain, the agent's copy of the config, and a current token. Bootstrap sends each host a copy of the operator's config on stdin and installs it as the `agent` user's `tbhbdev/config.toml`, mode 0600 in a 0700 directory. It reports `ok` when the copy already matches and `changed` otherwise, and never prints the file or a diff. Before the upgrade, it generates the private gitleaks config from the private repositories of each `[gitleaks] owners` entry, the tailnet, the vaults, the Connect endpoints, the operator's account names and each path in the optional `[gitleaks] session_sockets`, and publishes it to the `config` field of the item `gitleaks-private` in the vault `[vaults] all`, reporting `ok` or `changed`. A source that fails stops bootstrap before it writes anything. A second run doesn't change anything.
- `upgrade [--host H ... | --all]` brings a bootstrapped host up to date. It doesn't need a terminal, Touch ID, `op`, or a sudo password, so agents run it. Each step reports `ok`, `changed`, or `failed`. It clones the workspace repositories as the agent's GitHub account and fast-forwards clean clones on their default branch. Then it runs `mise install` at the workspace root and checks that the host's token still reaches its vaults through Connect. Last, the `gitleaks-config` step reads the private gitleaks config from that field through Connect with the host's own token. If the text extends `.gitleaks.toml` and defines the `tbhb-private-canary` rule, the step installs it as `.config/gitleaks/private.toml` in the agent's home and in the operator's: each a 0600 file in a 0700 directory, replaced atomically. Otherwise it fails without writing anything.
- `grant` and `revoke` take `[--host H ... | --all] VAULT ...` and swap the host's token for one with the new vault set. `revoke` refuses to take a host's own vault or `[vaults] all` from it.
- `status` reports each host's token name, vaults, and expiry, whether its keychain entry exists, whether its copy of the config matches yours, and whether `devel` has its workspace access. It never prints the token.
- `exec [--host H] [--operator] -- CMD ...` runs a command on a host as the `agent` user. With `--operator` it runs the command as you, after a Touch ID check.

Bootstrap's copy sets `[connect] endpoints` to `https://<host>.<suffix>` for each of `[connect] server_hosts`, in order. Its suffix is the tailnet's MagicDNS suffix, the `MagicDNSSuffix` that `tailscale status --json` reports, so these commands need `tailscale` on the operator's Mac. That suffix stays out of every argument and message, and lives only in each host's 0600 copy. When `tailscale` is missing, fails, or doesn't report a suffix, bootstrap doesn't write a copy on any host. Any endpoints in the operator's own file give way to the computed list. The copy keeps values but drops comments and layout, so the file can contain only tables of strings and lists of strings. Grant and revoke check a new token against the same endpoints, and `status` compares each host's file with the same copy.

Without `--host` or `--all`, a command acts on this machine. Bootstrap, grant, and revoke create a new token when the host has none or its token covers other vaults. They also replace a token that expires within 14 days, or that the host's keychain lacks. The tool passes a token to other programs only on stdin. It never puts one in `argv`, a file, or a log.

Bootstrap, grant, and revoke start with a Touch ID check.

Bootstrap creates the group `devel` with group ID 33835 and the user `agent` with user ID 24368, with `devel` as its primary group, no password login, and a home of mode 0700. On macOS it uses `dscl .` and hides the user from the login window. It never changes an existing account, and an account with other IDs stops that host with an error, as does a user or group ID that another name already uses. Bootstrap runs its root steps with `sudo -n`. When sudo needs a password, it runs them through `ssh -t`, so sudo asks once per host.

The user config's `[workspace]` table names the workspace with `[workspace] root`, a path below the operator's home, and `repos`, a list of `owner/name`. With one, bootstrap:

- lets `devel` traverse each directory from the home down to the root
- gives `devel` inheritable read, write, and create access on the root and everything below it
- adds only missing entries, and reapplies the root's entries below the root only when the root lacked them or `--repair-acl` asks
- ends with a read and write probe as the agent

Bootstrap and upgrade skip the workspace when the config has no `[workspace]`.

---
description: Git hooks, user-level harness hooks, the GitHub command-line tool shim, and the ccstatusline pin.
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
title: Install
type: Reference
---

# Install

`install` installs the git hooks and the user-level harness hooks.
`--hooks-only` installs only this clone's git hooks without writing user-level config.

Run `tbhbdev alpha install` once on each host, from a clone of a tbhb-dev repository.
It does seven things:

- It installs that clone's git hooks into `$(git rev-parse --git-common-dir)/hooks`, which every worktree of the clone shares.
  The pre-push guard runs before prek and refuses pushes when `tbhbdev` is missing from PATH. prek runs pre-commit and pre-push, and three shims run `tbhbdev alpha identity` for prepare-commit-msg, commit-msg and post-commit.
  Without `tbhbdev` on PATH, the shims block a commit in a tbhb-dev repository and do nothing elsewhere.
- It merges the hooks into each harness's user-level config: `settings.json` in `$CLAUDE_CONFIG_DIR` or `.claude` in `$HOME` for Claude Code, `hooks.json` in `$CODEX_HOME` or `.codex` in `$HOME` for Codex, and `.gemini/config/hooks.json` in `$HOME` for Antigravity.
- It records Codex's trust for the hooks it wrote.
  Codex runs a hook only when `config.toml` in `$CODEX_HOME` holds a trusted hash for it, so install sets `trusted_hash` under `[hooks.state."<key>"]` for each hook of its own, which is the trust `/hooks` grants a person.
  Entries for other hooks are unchanged, and so is the rest of `config.toml`, comments included.
  A hook whose command changes loses its trust, since the hash covers the command.
  The hash for a group with no matcher (`SessionStart`, `SubagentStart`) leaves `matcher` out, which nobody has checked against Codex yet.
  A live check after merge confirms it.
  If Codex hashes `"matcher": null` or `""` instead, those two hooks show as untrusted again.
  When a hook of its own moves to a new position, install removes the entry for the old one.
  A new `config.toml` is readable by the user only, an existing one keeps its mode, and one that isn't valid TOML is reported and left alone.
  The `SSH_AUTH_SOCK` setting below edits the same `config.toml` right after, so one run applies both, trust first, and each keeps the other's keys.
- When an installed plugin supplies `claude-private.md` or `mise-private.toml`, it writes them to `.claude/CLAUDE.private.md` and `.config/mise/conf.d/private.toml` in `$HOME`, readable by the user only.
  When no plugin supplies a file, it skips that file.
- It sets `SSH_AUTH_SOCK` to the empty string for every agent command, in the `env` object of Claude Code's `settings.json` and in `shell_environment_policy.set` of Codex's `config.toml`, so an agent's command doesn't connect to the 1Password SSH agent and prompt the operator.
  Host provisioning sets the variable for the account an agent runs as, but gives the operator's own account no harness environment, so `install` is the only thing that sets it there.
  It keeps every other key of both files, and the comments and layout of `config.toml`.
- It writes the `gh` shim, an executable `gh` in `$XDG_DATA_HOME/tbhbdev/agent-bin`, where `XDG_DATA_HOME` defaults to `.local/share` in the home directory.
  It also writes an `ssh` there that runs the next `ssh` on `PATH` with `IdentityAgent` set to `none`, so ssh in an agent session doesn't reach the 1Password SSH agent, whatever the user's ssh config says.
  `BatchMode` stays the caller's choice.
  A `SSH_AUTH_SOCK` that isn't the 1Password socket is kept, and the caller's own `-o IdentityAgent` doesn't override the shim.
  The installer leaves an `ssh` there that it didn't write.
  The `scp` and `sftp` on macOS and Ubuntu start `/usr/bin/ssh` directly, and `-J` starts a second ssh, so those paths skip the shim.
  Claude Code's SessionStart hook puts the directory first on `PATH`, so a session started before the install doesn't get it.
- It pins `ccstatusline` for Claude Code.
  A `statusLine.command` of exactly `bunx -y ccstatusline@latest`, with or without the `PATH` and `COLUMNS` prefix, asks the registry on every refresh.
  Install replaces it with a pinned copy.
  Each run stages `bun add --exact --ignore-scripts ccstatusline@2.2.33` in its own temporary directory beside `$XDG_DATA_HOME/tbhbdev/ccstatusline/2.2.33`, so two installers never share files.
  The staged package must pass the same check a later run applies.
  Then the directory moves into place.
  If `bun` fails or times out, no install appears at the final path, and the next run installs again.
  A run reuses an install only when the `.complete` marker names the version and the package files hash to a digest pinned in the code.
  A symlink on the path from the install root to the package fails the check, and so does any file change.
  A failed check makes the run install again.
  The command runs the installed script through `node`, and the `COLUMNS` fallback stays.
  Install backs up `settings.json` first and reads the written command back.
  It reads `settings.json` again just before the replace and aborts if the file differs from what install read first, so an edit made during the install stays and install reports it.
  An edit in the instant between that read and the replace disappears from the new file, and the backup keeps the earlier text.
  Install rewrites a command it wrote earlier, whatever the `node` path or version, but only when the command matches the installer's own form exactly.
  That form has an absolute `node` path, the installer's quoting and the script under a version directory of `ccstatusline`.
  A version bump replaces the old command on the next install, and the old version directory stays on disk.
  You can delete it.
  Install leaves the other keys of `settings.json` alone, along with any other command and any config with no status line.
  Without `node` on `PATH` it prints why and leaves the old command.
  Without `bun` it does the same, unless a complete install exists, and then it replaces the command without `bun`.
  The registry metadata for 2.2.33 has an empty dependency list (`npm view ccstatusline@2.2.33 dependencies`), and a clean `bun add` of it creates only `node_modules/ccstatusline`.
  The exact version plus the digest cover everything the install fetches.

It never prompts, runs `op` or needs the 1Password app, so configuration management can run it unattended, and a second run doesn't change anything.
Each hook command it writes ends in `# tbhbdev alpha install`.
A rerun replaces only those entries, and keeps everything else in place.
Before it changes a config, `config.toml` included, it saves the old one beside it as `<name>.tbhbdev-backup.<time>`.
`--dry-run` prints the changes without writing them, and `--harness claude|codex|antigravity` limits the merge to one harness.
`--hooks-only` installs only this clone's git hooks without writing user-level config and accepts `--dry-run` but rejects `--hook` and `--harness`.

For Claude Code it also installs `state-guard` (PreToolUse for Bash, Write and Edit, plus SessionStart) and `session-context` (SessionStart), each with a 10 second timeout.
Their private values come from an optional `[harness]` table in the user config, and a missing table or key turns off the check that needs it, never blocks:

```toml
[harness]
rule = "Example rule: a status claim carries its evidence."   # SessionStart text
decisions_dir = "/example/decisions"   # the path the open-note block names
open_note = "/example/Open.md"         # a path suffix; a new item needs "Decisions checked:"
hosts = ["example-a=peer Mac", "example-b=Linux VM", "example-c"]   # "name=role"
```

The commit check runs without config.
The command guard checks arguments for trailers and sign-off flags.
Message bodies in here documents or `-F` files pass this guard.
The commit-msg identity check catches trailers in those bodies.

These user-level hooks run in every repository.
Before acting, the identity, pull request, approval guard, and install hooks check that `origin` names a tbhb-dev repository on GitHub.
Outside a repository, the identity record hook still writes the session's record to the user's store under `~/.local/state/tbhbdev/tbhb-alpha`.
Both `state-guard` and `session-context` run regardless of origin.
At SessionStart, Claude Code runs `tbhbdev alpha install --hook`, which puts the `gh` shim first on PATH for the session's commands and reinstalls the git hooks of the worktree the session starts in.
Its PostModelSwitch hook records a `/model` switch, so a commit in the next turn credits the new model.

The alpha commands write each worktree's state under `$(git rev-parse --git-dir)/tbhb-alpha`, so sessions in two worktrees of one clone keep separate state.

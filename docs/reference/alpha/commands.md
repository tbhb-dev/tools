---
description: Interim agent commands, installation, and harness hooks.
generated:
  - role: generated
    harness: codex
    model: openai:gpt-6
    at: "2026-11-12T18:44:55Z"
status: stable
tags: [alpha]
title: Alpha commands
type: Reference
---

# Alpha commands

`tbhbdev.alpha.repo_token` is a library for the ops deployer. It uses explicit Connect credentials to request a token restricted to one repository and run Git with that token in memory.

`tbhbdev alpha` holds the interim agent tools that the tbhb-dev repositories used to carry in `tools/interim`:

- `pre-push-guard REMOTE URL` refuses non-fast-forward pushes and scans public pushes for private names in commit messages, added lines, and added or renamed paths. The installed pre-push hook runs it before prek with the same arguments and input. Public pushes require a readable, nonempty `~/.local/state/tbhb/private-repos.txt` alongside `[pr_check] private_repos`. The list’s header defines words that only match in repository paths. Nonempty `SKIP` or `PREK_SKIP` refuses the push. Install fails when a foreign pre-push hook exists and asks you to move it aside. No setting bypasses the guard. Git’s `--no-verify` skips all hooks. Standing rules prohibit it.
- `identity` records which agent and model made each edit and writes the `Generated-by` trailers.
- `guard-op` requires the configured `[op] account` flag on shell calls and proposed writes in every directory. `PreToolUse` covers Claude `Bash` and `Write|Edit|MultiEdit|NotebookEdit`, Codex `Bash`/`shell`/`apply_patch` and Antigravity `run_command`/edit tools. The guard scans shell files, Markdown shell fences, Python command expressions, notebook code cells, and YAML command fields. It allows unrelated edits to existing offenders. `OP_ACCOUNT` alone doesn't pass the guard. The module docstring lists the allowed command forms that the scanner misses. Check the Codex `apply_patch` PreToolUse matcher live after install. Its behavior remains unverified against Codex's documentation.
- `guard-gh-pr` is a PreToolUse hook that blocks `gh pr create`, `gh pr edit` and `gh pr merge`.
- `state-guard` is a Claude Code hook that refuses a status claim with no evidence in a SendMessage call, or tags it when the user config turns tagging on. It blocks these claims in a final reply, and a `git commit` that adds a sign-off or co-author trailer.
- `session-context` is a Claude Code SessionStart hook that reports the host, its role, the other hosts, and the local time.
- `guard-gh-poll` is a PreToolUse hook that blocks GitHub polling and unsetting the 1Password service account token's variable.
- `guard-scripts` is a PreToolUse hook that keeps scripts for the operator in `~/.local/libexec/tbhb/`. It reads the common ways an agent writes a file, so it misses the rest. It denies a script written anywhere else under the home directory, `/tmp` or `/private/tmp`, and any script in a drafts repository. Git work trees, this session's scratchpad and a run-scoped `$TMPDIR` pass. Name each file `YYYY-MM-DD-<owner>-<slug>[.vN].sh` or `YYYY-MM-DD-<owner>-<slug>[.vN].py`. The guard refuses a write to an existing file. Give each revision a new name. Both suffixes and any content starting with `#!` count as scripts. When the hook can read the content, a new `.py` file there must start with exactly `#!/usr/bin/env -S python3 -I`. Make it executable with `chmod 0755`. Known gaps include unknown content (such as `echo` output) and the executable bit, which the hook can't check at write time. Run logs go in `~/.local/state/tbhb/<same name>/`.
- `gh-shim` is the `gh` that agent sessions find first on PATH. It budgets their `gh` calls as a backstop.
- `guard-gh-config-dir` is a PreToolUse hook that blocks `gh` commands when `GH_CONFIG_DIR` is unset or empty in the hook's environment. It scans the command string without running `gh` and accepts any nonempty value.
- `agent-env` runs Codex or Antigravity with the `gh` shim first on PATH.
- `pr` checks `.github/PULL_REQUEST_DRAFT.md` and opens or updates the pull request from it. `pr ready --pr N` marks it ready for review, only when the pull request's GitHub author is the account the agent's own `gh` runs as. `pr rerun N` reruns only the failed jobs of the latest run for pull request N's current head commit. It reports an error when that run hasn't failed or no run exists. `pr close N --reason TEXT` (or `--comment TEXT`) closes it after the agent-session, open-state and opened-ledger checks, in origin's repository. `pr delete-branch N` deletes only the head branch of a closed or merged pull request in origin's repository on github.com. It refuses fork heads, `main`, protected branches, and unreadable metadata.
- `review` drafts and checks a pull request review.
- `gh-app` requests a GitHub App installation token, reading the App's id and RSA key through Connect, and keeps it under `$XDG_STATE_HOME/gh-app/VAULT/APP`. The command serves a cached token with more than ten minutes left, even during a cooldown, and a cooldown refuses any other request. Only a 404 sends the installation lookup from the repository to its owner. The command syncs each file to disk before it replaces the old one.
- `repo` holds repository commands. `repo bootstrap` applies the standard repository settings.
- `merge` squash-merges an approved pull request.
- `loop` validates its user configuration and refuses live intake until adapters connect the runner to its sources. Its shadow runner accepts typed event and reconcile interfaces for simulation. The package includes pure state derivation and rules that produce typed shadow intents with action keys and spawn budgets. A SQLite store records receipts, action keys, runs, and escalation markers.
- `agent precheck CLONE --reference REF [--host HOST]` compares a clone's hooks with a local reference and checks its Git config for risky keys. Paths may be absolute or relative to the home directory. Collection uses one SSH call with `--host` and reads locally otherwise. It never runs Git. Exit codes are 0 for a match, 1 for mismatches, and 2 for usage or collection errors. The Python API `precheck(clone_data, reference)` returns the result for callers.
- `agent` coordinates runs with `exec`, `wait`, `status`, `log`, `diff`, `kill`, `clean` and `keep`. `clear-taint HOST --reason TEXT` records local host clearance. The package also includes the run request's schema and the repo argument rules.
- `install` installs the git hooks and the user-level harness hooks. `--hooks-only` installs only this clone's git hooks without writing user-level config.

`tbhbdev alpha pr checks --pr N` reads pull request N in the current repository. It prints the head commit hash on the first line, then one `name: CONCLUSION` line per check, including commit statuses. Unfinished checks (`PENDING`, `QUEUED`, `IN_PROGRESS`, `WAITING`, `REQUESTED` or a pending commit status) print `PENDING`. Exit code 0 means every check is green: `SUCCESS`, `NEUTRAL` or `SKIPPED`. Exit code 1 means a failed check, any value the command doesn't know (including lowercase ones, a commit status of `EXPECTED`, or a completed check with no conclusion), or no checks at all. Exit code 3 means nothing failed but a check is still unfinished. A check run that hasn't completed reads its status and never its conclusion. A commit status is green only for `SUCCESS`. A missing pull request, a failed read or a check with no name exits nonzero with an error.

`tbhbdev alpha verify-range BASE TIP [-C PATH]` checks each commit in `BASE..TIP`, oldest first. A commit passes when the agent is its author and committer, it has a signature header and valid attribution trailers, and it has exactly one parent. Sign-off trailers, co-author trailers and GitHub's no-reply addresses fail it. Exit codes are 0 when every commit passes, 1 when any fails and 2 for usage or Git errors.

`tbhbdev alpha brief new RUN --repo OWNER/REPO` writes a peer-run brief skeleton to stdout, or to a file with `-o FILE`. Fill its placeholders, then run `tbhbdev alpha brief check FILE...` to check the completed brief. Use `--branch` and `--base` to override the worktree defaults. `--push --scan-command CMD` includes the required outgoing scan command supplied by the operator. The command must be non-empty and contain no newline or backtick. The check normalizes Unicode, whitespace and case before searching for forbidden instructions and removing fixed sentences in their generated sections. It matches phrases by spelling and section placement. Authors can add lines in Hard rules. Use it to catch omissions and common mistakes. Review the brief's intent separately.

Run `tbhbdev alpha install` once on each host, from a clone of a tbhb-dev repository. It does six things:

- It installs that clone's git hooks into `$(git rev-parse --git-common-dir)/hooks`, which every worktree of the clone shares. The pre-push guard runs before prek and refuses pushes when `tbhbdev` is missing from PATH. prek runs pre-commit and pre-push, and three shims run `tbhbdev alpha identity` for prepare-commit-msg, commit-msg and post-commit. Without `tbhbdev` on PATH, the shims block a commit in a tbhb-dev repository and do nothing elsewhere.
- It merges the hooks into each harness's user-level config: `settings.json` in `$CLAUDE_CONFIG_DIR` or `.claude` in `$HOME` for Claude Code, `hooks.json` in `$CODEX_HOME` or `.codex` in `$HOME` for Codex, and `.gemini/config/hooks.json` in `$HOME` for Antigravity.
- It records Codex's trust for the hooks it wrote. Codex runs a hook only when `config.toml` in `$CODEX_HOME` holds a trusted hash for it, so install sets `trusted_hash` under `[hooks.state."<key>"]` for each hook of its own, which is the trust `/hooks` grants a person. Entries for other hooks are unchanged, and so is the rest of `config.toml`, comments included. A hook whose command changes loses its trust, since the hash covers the command. The hash for a group with no matcher (`SessionStart`, `SubagentStart`) leaves `matcher` out, which nobody has checked against Codex yet. A live check after merge confirms it. If Codex hashes `"matcher": null` or `""` instead, those two hooks show as untrusted again. When a hook of its own moves to a new position, install removes the entry for the old one. A new `config.toml` is readable by the user only, an existing one keeps its mode, and one that isn't valid TOML is reported and left alone. The `SSH_AUTH_SOCK` setting below edits the same `config.toml` right after, so one run applies both, trust first, and each keeps the other's keys.
- When an installed plugin supplies `claude-private.md` or `mise-private.toml`, it writes them to `.claude/CLAUDE.private.md` and `.config/mise/conf.d/private.toml` in `$HOME`, readable by the user only. When no plugin supplies a file, it skips that file.
- It sets `SSH_AUTH_SOCK` to the empty string for every agent command, in the `env` object of Claude Code's `settings.json` and in `shell_environment_policy.set` of Codex's `config.toml`, so an agent's command doesn't connect to the 1Password SSH agent and prompt the operator. Host provisioning sets the variable for the account an agent runs as, but gives the operator's own account no harness environment, so `install` is the only thing that sets it there. It keeps every other key of both files, and the comments and layout of `config.toml`.
- It writes the `gh` shim, an executable `gh` in `$XDG_DATA_HOME/tbhbdev/agent-bin`, where `XDG_DATA_HOME` defaults to `.local/share` in the home directory.

It never prompts, runs `op` or needs the 1Password app, so configuration management can run it unattended, and a second run doesn't change anything. Each hook command it writes ends in `# tbhbdev alpha install`. A rerun replaces only those entries, and keeps everything else in place. Before it changes a config, `config.toml` included, it saves the old one beside it as `<name>.tbhbdev-backup.<time>`. `--dry-run` prints the changes without writing them, and `--harness claude|codex|antigravity` limits the merge to one harness. `--hooks-only` installs only this clone's git hooks without writing user-level config and accepts `--dry-run` but rejects `--hook` and `--harness`.

For Claude Code it also installs `state-guard` (PreToolUse for Bash, SendMessage, Write and Edit, plus Stop and SessionStart) and `session-context` (SessionStart), each with a 10 second timeout. Their private values come from an optional `[harness]` table in the user config, and a missing table or key turns off the check that needs it, never blocks:

```toml
[harness]
rule = "Example rule: a status claim carries its evidence."   # SessionStart text
decisions_dir = "/example/decisions"   # the path the open-note block names
open_note = "/example/Open.md"         # a path suffix; a new item needs "Decisions checked:"
hosts = ["example-a=peer Mac", "example-b=Linux VM", "example-c"]   # "name=role"
```

`tbhbdev alpha pr check` reads one more optional table. `private_repos` lists private repository names that must not appear in a public repository's draft:

```toml
[pr_check]
private_repos = ["example-lab", "example-notes"]   # optional
```

The check flags a whole repository-name token in the title and body that equals a listed name, and any token ending in `-private`, such as `example-private`. The suffix rule works without config. Matching ignores case and a trailing `.git`. A clone URL matches. It ignores substrings of longer names. A token that starts with a hyphen, such as `--private`, passes, but the check flags a word such as `non-private`. It skips a working repository whose own name ends in `-private` or appears in the list. If `origin` is unreadable, the check runs. It covers the title and body only, not commit messages.

Commit and claim checks run without config. For commits, the command guard checks arguments for trailers and sign-off flags. Message bodies in here documents or `-F` files pass this guard. The commit-msg identity check catches trailers in those bodies.

Evidence can be a `#N` reference or a commit hash of 7 to 40 hex characters. Hashes must contain a digit and a letter from a to f. A log path or inline code can supply evidence as well, as can command output. Claims can attribute a result with `says` or `reports`, or mark it `unverified`.

On SendMessage, the guard refuses a message with an unsupported claim unless the user config sets `[state_guard] tag_messages = true`. With that setting, on PreToolUse it inserts `[state-guard: unverified]` before each sentence it flags, preserving the other input fields. A sender-written tag can't replace the guard's tag. The guard judges each message twice, once with every tag or tag fragment replaced by a space and once with them removed, so a tag nested inside another tag, written in another case, split by whitespace or invisible characters, or placed inside a word doesn't hide a claim. A claim in either rendering counts, and evidence counts only when both renderings have it. The guard puts its tag before the sentence's first word, so a sender's own markup, such as a bold label or an HTML comment around the sentence, can still sit in front of it and hide it from a reader. Each tagged sentence goes to `$XDG_STATE_HOME/tbhbdev/state-guard/tagged.jsonl`, where the state directory defaults to `~/.local/state`. Any other value, a config error, another hook event, or a tagged message whose hook output exceeds 4096 bytes, newline included, keeps the refusal, with the same single stderr message as with tagging off. The log opens without following symlinks in the last directory or the file, and only as a single-link regular file. A log failure of any kind is silent and the message still goes out, because the log isn't the verdict. The log write has no time limit on a regular file that's slow to respond, and a log that's a named pipe, which opens without blocking, fails silently. With tagging off, the guard also ignores a sender-written tag when it judges a message. The Stop hook still blocks final replies with claims that lack evidence.

These user-level hooks run in every repository. Before acting, the identity, pull request, approval guard, and install hooks check that `origin` names a tbhb-dev repository on GitHub. Outside a repository, the identity record hook still writes the session's record to the user's store under `~/.local/state/tbhbdev/tbhb-alpha`. Both `state-guard` and `session-context` run regardless of origin. At SessionStart, Claude Code runs `tbhbdev alpha install --hook`, which puts the `gh` shim first on PATH for the session's commands and reinstalls the git hooks of the worktree the session starts in. Its PostModelSwitch hook records a `/model` switch, so a commit in the next turn credits the new model.

The alpha commands write each worktree's state under `$(git rev-parse --git-dir)/tbhb-alpha`, and each worktree has its own pull request draft, so sessions in two worktrees of one clone don't overwrite each other.

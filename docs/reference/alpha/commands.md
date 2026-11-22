---
description: Interim agent commands, installation, and harness hooks.
generated:
- role: generated
  effort: high
  harness: codex:0.164.0
  model: openai:gpt-6.1-sol
  session: sha-256-64:9fa208639bb640db
  surface: exec
  at: '2026-11-15T19:18:44Z'
- role: generated
  effort: high
  harness: codex:0.164.0
  model: openai:gpt-6.1-sol
  session: sha-256-64:e641a1b7ee82fd67
  surface: exec
  at: '2026-11-15T19:08:28Z'
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
- `guard-gh-pr` is a PreToolUse hook that blocks `gh pr create`, `gh pr edit`, `gh pr merge` and `gh pr ready`, and names the `tbhbdev alpha` command to run instead.
- `state-guard` is a Claude Code hook that blocks a `git commit` that adds a sign-off or co-author trailer, and a new open-note item without a `Decisions checked:` line. It doesn't judge status claims in replies or messages.
- `session-context` is a Claude Code SessionStart hook that reports the host, its role, the other hosts, and the local time.
- `guard-gh-poll` is a PreToolUse hook that blocks GitHub polling and unsetting the 1Password service account token's variable. It allows `gh run list --limit 1` and a list endpoint with `per_page=1`.
- `guard-scripts` is a PreToolUse hook that keeps scripts for the operator in `~/.local/libexec/tbhb/`. It reads the common ways an agent writes a file, so it misses the rest. It denies a script written anywhere else under the home directory, `/tmp` or `/private/tmp`, and any script in a drafts repository. Git work trees, this session's scratchpad and a run-scoped `$TMPDIR` pass. Name each file `YYYY-MM-DD-<owner>-<slug>[.vN].sh` or `YYYY-MM-DD-<owner>-<slug>[.vN].py`. The guard refuses a write to an existing file. Give each revision a new name. Both suffixes and any content starting with `#!` count as scripts. When the hook can read the content, a new `.py` file there must start with exactly `#!/usr/bin/env -S python3 -I`. Make it executable with `chmod 0755`. Known gaps include unknown content (such as `echo` output) and the executable bit, which the hook can't check at write time. Run logs go in `~/.local/state/tbhb/<same name>/`.
- `gh-shim` is the `gh` that agent sessions find first on PATH. It budgets their `gh` calls as a backstop.
- `guard-gh-poll` also denies git hook bypasses: `git -c core.hooksPath`, writing `core.hooksPath` and `--no-verify`. Reads of the key pass.
- `guard-gh-config-dir` is a PreToolUse hook that blocks `gh` commands when `GH_CONFIG_DIR` is unset or empty in the hook's environment. It scans the command string without running `gh` and accepts any nonempty value.
- `agent-env` runs Codex or Antigravity with the `gh` shim first on PATH.
- `pr check [DRAFT]` checks a draft, and `pr create [DRAFT]` opens a pull request from it. Both default to `.github/PULL_REQUEST_DRAFT.md`. `pr edit DRAFT --pr N` updates pull request N and requires the draft path. `pr ready N [N...]` marks each one ready for review, only when the pull request's GitHub author is the account the agent's own `gh` runs as. `pr rerun N` reruns only the failed jobs of the latest run for pull request N's current head commit. It reports an error when that run hasn't failed or no run exists. `pr close N --reason TEXT` (or `--comment TEXT`) closes it after the agent-session, open-state and opened-ledger checks, in origin's repository. `pr delete-branch N` deletes only the head branch of a closed or merged pull request in origin's GitHub repository. It refuses fork heads, `main`, protected branches, and unreadable metadata.
- `review` drafts and checks a pull request review.
- `review policy check [PATH] [--json]` checks the review files of the worktree at PATH, which defaults to the current directory. It reads the files as data and applies none of them. A missing file passes, because the floor and its defaults apply. It checks `.github/pull_request_review.toml` against a closed schema (`config-toml`, `config-schema`). It checks that the template keeps the floor's required sections in order and gives each section a guidance comment (`template-floor-section`, `template-guidance`). It checks that the draft schema is valid JSON Schema 2020-12 and sets `additionalProperties` to false (`draft-schema-invalid`, `draft-schema-open`). It checks that checklist item ids are unique and outside `R1` to `R99`, and that escalation ids are unique and don't start with `FE` (`checklist-frontmatter`, `checklist-item-id`, `checklist-escalate-id`). An unreadable file is `unreadable`. The checklist's other frontmatter keys aren't validated yet. Output is one `PATH: RULE: MESSAGE` line per finding, or with `--json` an object `{"ok": bool, "findings": [{"path", "id", "message"}]}`. Exit 0 means no findings, 1 means findings, and 2 means PATH isn't a directory or the floor fails to load. With `--json`, exit 2 still prints one object, `{"ok": false, "error": KIND, "message": TEXT}`, where KIND is `not-a-directory` or `floor-load`.
- Run-host posts a review run's verdict itself, after the run exits 0 (a failed post exits 5, and the log names why). It reads the run's one `.git/tbhb-alpha/review-N.md` draft, checks it again with the `review check` rules minus the prose linters (a run host lacks them), and requires `commit_id` to equal the commit the run reviewed. It holds the draft to the floor's schema and sections only, and never reads the repository's policy or schema from the writable worktree. A draft with a repeated frontmatter key is refused. Before posting it checks that the pull request's head is still the judged commit, and it returns the existing review if one already carries the run's stamp. Only `COMMENT` and `REQUEST_CHANGES` post; `APPROVE` is refused. Any 2xx answer counts as posted. The review body ends with the run name, harness, model and judged head. `review create N --repo REPO --commit SHA --stamp TEXT` is the same step as a command: it posts the worktree's draft with a token read from stdin. The reviews App's token for that one repository, limited to `pull_requests: write`, is minted on the host and never enters the sandbox. The result goes to the run's `review` file as `REPO#N REVIEW_ID`.
- `version-check [--host NAME ...] [--expect SHA|latest] [--json]` compares the installed build on each host, read-only. For each host it runs `$HOME/.local/bin/tbhbdev --version` over `ssh -n -o BatchMode=yes -o ConnectTimeout=5` (directly on this host) and prints `<host> <sha-or-unknown> <state>`, where the state is `ok`, `stale`, `unknown` or `unreachable`. `--json` prints a list of objects with `host`, `version` and `state`. Hosts come from `--host` or the names in `[harness] hosts` of the user config, and exits 2 with neither source. `--expect SHA` compares every host to that 40-hex commit, `--expect latest` to the head of origin's main, and with neither the reference is the build most hosts run (the first host's on a tie). It exits 0 when every host is `ok`, 1 when any is stale or unknown, 2 for a usage error, and 3 when a host is unreachable and none is stale (or `latest` is unavailable). The build is the commit in the `direct_url.json` of the install, else the 40-hex `COMMIT` of `tbhbdev/_build.py`, a stamp the repository never commits and that nothing writes yet, so a wheel install without one reports `unknown`.
- `gh-app` requests a GitHub App installation token, reading the App's id and RSA key through Connect, and keeps it under `$XDG_STATE_HOME/gh-app/VAULT/APP`. The command serves a cached token with more than ten minutes left, even during a cooldown, and a cooldown refuses any other request. Only a 404 sends the installation lookup from the repository to its owner. The command syncs each file to disk before it replaces the old one.
- `repo` holds repository commands. `repo bootstrap` applies the standard repository settings.
- `merge` squash-merges an approved pull request.
- `loop` validates its user configuration and refuses live intake until adapters connect the runner to its sources. Its shadow runner accepts typed event and reconcile interfaces for simulation. The package includes pure state derivation and rules that produce typed shadow intents with action keys and spawn budgets. A SQLite store records receipts, action keys, runs, and escalation markers.
- `agent precheck CLONE --reference REF [--host HOST]` compares a clone's hooks with a local reference and checks its Git config for risky keys. Paths may be absolute or relative to the home directory. Collection uses one SSH call with `--host` and reads locally otherwise. It never runs Git. Exit codes are 0 for a match, 1 for mismatches, and 2 for usage or collection errors. The Python API `precheck(clone_data, reference)` returns the result for callers.
- `agent` coordinates runs with `exec`, `wait`, `status`, `log`, `diff`, `kill`, `clean` and `keep`. `clear-taint HOST --reason TEXT` records local host clearance. The package also includes the run request's schema and the repo argument rules.
- `install` installs the git hooks and the user-level harness hooks. `--hooks-only` installs only this clone's git hooks without writing user-level config.

`tbhbdev alpha pr check` runs the rules size counter after linting the draft, comparing `origin/main` with `HEAD`. It prints the added code and raw line counts with their limits: 400 code lines and 1,500 raw lines. If either count exceeds its limit, the check fails with a message to split the change into stacked pull requests. When both counts exceed their limits, the problem reports the code count. If the counter is unavailable or returns an error or invalid JSON, the command prints `size count unavailable: <reason>` as a warning and continues with the draft result.

`tbhbdev alpha pr checks N` reads pull request N in the current repository. It prints the head commit hash on the first line, then one `name: CONCLUSION` line per check, including commit statuses. Unfinished checks (`PENDING`, `QUEUED`, `IN_PROGRESS`, `WAITING`, `REQUESTED` or a pending commit status) print `PENDING`. Exit code 0 means every check is green: `SUCCESS`, `NEUTRAL` or `SKIPPED`. Exit code 1 means a failed check, any value the command doesn't know (including lowercase ones, a commit status of `EXPECTED`, or a completed check with no conclusion), or no checks at all. Exit code 3 means nothing failed but a check is still unfinished. A check run that hasn't completed reads its status and never its conclusion. A commit status is green only for `SUCCESS`. A missing pull request, a failed read or a check with no name exits nonzero with an error.

`tbhbdev alpha pr-stats OWNER/REPO [--limit N]` prints one JSON row for each merged or closed pull request. The cycle time starts at the earliest of every `started_at` on its identity lines and its earliest commit's committer date, and ends at `mergedAt` or, for a pull request closed without merging, `closedAt`. A row's `start_source` is `started_at` or `commit`, and its `state` is `merged` or `closed`.

`tbhbdev alpha verify-range BASE TIP [-C PATH]` checks each commit in `BASE..TIP`, oldest first. A commit passes when the agent is its author and committer, it has a signature header and valid attribution trailers, and it has exactly one parent. Sign-off trailers, co-author trailers and GitHub's no-reply addresses fail it. Exit codes are 0 when every commit passes, 1 when any fails and 2 for usage or Git errors.

`tbhbdev alpha brief new RUN --repo OWNER/REPO` writes a peer-run brief skeleton to stdout, or to a file with `-o FILE`. Fill its placeholders, then run `tbhbdev alpha brief check FILE...` to check the completed brief. Use `--branch` and `--base` to override the worktree defaults. `--push --scan-command CMD` includes the required outgoing scan command supplied by the operator. The command must be non-empty and contain no newline or backtick. The check normalizes Unicode, whitespace and case before searching for forbidden instructions and removing fixed sentences in their generated sections. It matches phrases by spelling and section placement. Authors can add lines in Hard rules. Use it to catch omissions and common mistakes. Review the brief's intent separately.

Run `tbhbdev alpha install` once on each host, from a clone of a tbhb-dev repository. It does seven things:

- It installs that clone's git hooks into `$(git rev-parse --git-common-dir)/hooks`, which every worktree of the clone shares. The pre-push guard runs before prek and refuses pushes when `tbhbdev` is missing from PATH. prek runs pre-commit and pre-push, and three shims run `tbhbdev alpha identity` for prepare-commit-msg, commit-msg and post-commit. Without `tbhbdev` on PATH, the shims block a commit in a tbhb-dev repository and do nothing elsewhere.
- It merges the hooks into each harness's user-level config: `settings.json` in `$CLAUDE_CONFIG_DIR` or `.claude` in `$HOME` for Claude Code, `hooks.json` in `$CODEX_HOME` or `.codex` in `$HOME` for Codex, and `.gemini/config/hooks.json` in `$HOME` for Antigravity.
- It records Codex's trust for the hooks it wrote. Codex runs a hook only when `config.toml` in `$CODEX_HOME` holds a trusted hash for it, so install sets `trusted_hash` under `[hooks.state."<key>"]` for each hook of its own, which is the trust `/hooks` grants a person. Entries for other hooks are unchanged, and so is the rest of `config.toml`, comments included. A hook whose command changes loses its trust, since the hash covers the command. The hash for a group with no matcher (`SessionStart`, `SubagentStart`) leaves `matcher` out, which nobody has checked against Codex yet. A live check after merge confirms it. If Codex hashes `"matcher": null` or `""` instead, those two hooks show as untrusted again. When a hook of its own moves to a new position, install removes the entry for the old one. A new `config.toml` is readable by the user only, an existing one keeps its mode, and one that isn't valid TOML is reported and left alone. The `SSH_AUTH_SOCK` setting below edits the same `config.toml` right after, so one run applies both, trust first, and each keeps the other's keys.
- When an installed plugin supplies `claude-private.md` or `mise-private.toml`, it writes them to `.claude/CLAUDE.private.md` and `.config/mise/conf.d/private.toml` in `$HOME`, readable by the user only. When no plugin supplies a file, it skips that file.
- It sets `SSH_AUTH_SOCK` to the empty string for every agent command, in the `env` object of Claude Code's `settings.json` and in `shell_environment_policy.set` of Codex's `config.toml`, so an agent's command doesn't connect to the 1Password SSH agent and prompt the operator. Host provisioning sets the variable for the account an agent runs as, but gives the operator's own account no harness environment, so `install` is the only thing that sets it there. It keeps every other key of both files, and the comments and layout of `config.toml`.
- It writes the `gh` shim, an executable `gh` in `$XDG_DATA_HOME/tbhbdev/agent-bin`, where `XDG_DATA_HOME` defaults to `.local/share` in the home directory.
- It pins `ccstatusline` for Claude Code. A `statusLine.command` of exactly `bunx -y ccstatusline@latest`, with or without the `PATH` and `COLUMNS` prefix, asks the registry on every refresh. Install replaces it with a pinned copy. Each run stages `bun add --exact --ignore-scripts ccstatusline@2.2.33` in its own temporary directory beside `$XDG_DATA_HOME/tbhbdev/ccstatusline/2.2.33`, so two installers never share files. The staged package must pass the same check a later run applies. Then the directory moves into place. If `bun` fails or times out, no install appears at the final path, and the next run installs again. A run reuses an install only when the `.complete` marker names the version and the package files hash to a digest pinned in the code. A symlink on the path from the install root to the package fails the check, and so does any file change. A failed check makes the run install again. The command runs the installed script through `node`, and the `COLUMNS` fallback stays. Install backs up `settings.json` first and reads the written command back. It reads `settings.json` again just before the replace and aborts if the file differs from what install read first, so an edit made during the install stays and install reports it. An edit in the instant between that read and the replace disappears from the new file, and the backup keeps the earlier text. Install rewrites a command it wrote earlier, whatever the `node` path or version, but only when the command matches the installer's own form exactly. That form has an absolute `node` path, the installer's quoting and the script under a version directory of `ccstatusline`. A version bump replaces the old command on the next install, and the old version directory stays on disk. You can delete it. Install leaves the other keys of `settings.json` alone, along with any other command and any config with no status line. Without `node` on `PATH` it prints why and leaves the old command. Without `bun` it does the same, unless a complete install exists, and then it replaces the command without `bun`. The registry metadata for 2.2.33 has an empty dependency list (`npm view ccstatusline@2.2.33 dependencies`), and a clean `bun add` of it creates only `node_modules/ccstatusline`. The exact version plus the digest cover everything the install fetches.

It never prompts, runs `op` or needs the 1Password app, so configuration management can run it unattended, and a second run doesn't change anything. Each hook command it writes ends in `# tbhbdev alpha install`. A rerun replaces only those entries, and keeps everything else in place. Before it changes a config, `config.toml` included, it saves the old one beside it as `<name>.tbhbdev-backup.<time>`. `--dry-run` prints the changes without writing them, and `--harness claude|codex|antigravity` limits the merge to one harness. `--hooks-only` installs only this clone's git hooks without writing user-level config and accepts `--dry-run` but rejects `--hook` and `--harness`.

For Claude Code it also installs `state-guard` (PreToolUse for Bash, Write and Edit, plus SessionStart) and `session-context` (SessionStart), each with a 10 second timeout. Their private values come from an optional `[harness]` table in the user config, and a missing table or key turns off the check that needs it, never blocks:

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

The public rule IDs `mailgun-private-api-token`, `shopify-private-app-access-token`, and `tbhb-private-canary` pass unless listed as private names. This exemption ignores case and requires the whole token to match exactly.

The commit check runs without config. The command guard checks arguments for trailers and sign-off flags. Message bodies in here documents or `-F` files pass this guard. The commit-msg identity check catches trailers in those bodies.

These user-level hooks run in every repository. Before acting, the identity, pull request, approval guard, and install hooks check that `origin` names a tbhb-dev repository on GitHub. Outside a repository, the identity record hook still writes the session's record to the user's store under `~/.local/state/tbhbdev/tbhb-alpha`. Both `state-guard` and `session-context` run regardless of origin. At SessionStart, Claude Code runs `tbhbdev alpha install --hook`, which puts the `gh` shim first on PATH for the session's commands and reinstalls the git hooks of the worktree the session starts in. Its PostModelSwitch hook records a `/model` switch, so a commit in the next turn credits the new model.

The alpha commands write each worktree's state under `$(git rev-parse --git-dir)/tbhb-alpha`, and each worktree has its own pull request draft, so sessions in two worktrees of one clone don't overwrite each other.

## Identity start time

An identity line can record when its agent started and which launch it belongs to. Both attributes are optional. A line without them is still valid.

| Attribute | Value |
| --- | --- |
| `launch` | The `sha-256-64:` hash of the launcher's run id. |
| `started_at` | The agent's start time in Coordinated Universal Time with whole seconds, as `YYYY-MM-DDTHH:MM:SSZ`. The `T` and `Z` are capitals. |

Order is alphabetical:

```text
Generated-by: LLM effort=high;harness=codex:0.164.0;launch=sha-256-64:0a1b2c3d4e5f6071;model=openai:gpt-6.1-sol;session=sha-256-64:fb2bbc10b42a2d56;started_at=2026-11-14T00:02:41Z;surface=exec
```

The `record` hooks set both fields once in the session record, and a resumed session keeps its first values.

- A launcher writes `tbhb-alpha/launch.json` in the worktree's git directory, with a `launch` hash and a `started_at` time, before the harness starts. The first root session to run a hook there claims the file by adding its own hash as `session`, then copies both fields into its record. A later session finds the file claimed and inherits nothing from it.
- A session with no launch file to claim takes `started_at` from its first hook event and omits `launch`.
- A subagent takes `started_at` from the first hook event with its agent id, and inherits `launch` from its root session.

The `trailer` hook writes both attributes from the session record. Where lines for the same agent meet, on an amend, a rebase, or an interactive squash, the hook merges them. Lines with the same key and every attribute except `started_at` become one line, the one with the earliest `started_at`. Lines that differ in another attribute, including `effort`, remain distinct. A line with the attribute replaces one without it.

The `check` hook and `verify-range` accept both attributes. They require a real Coordinated Universal Time in the exact format for `started_at` and the hash format for `launch`. An agent commit whose `Generated-by` line lacks `started_at` passes with a warning.

The rules `commit_identity` check must accept both attributes before a host installs these hooks. Until it does, commits with them fail CI.

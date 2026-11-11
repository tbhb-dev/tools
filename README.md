# Shared tbhb-dev tools

This repository holds shared tools for tbhb-dev work: commands that more than one repository or session uses and that no one repository owns. Every tool is a subcommand of one Python package, `tbhbdev`.

## Running a tool

```sh
uv run tbhbdev <tool> [args...]
```

`tbhbdev` with no arguments lists the tools. After the tool's name come that tool's own arguments, and it exits with its own codes.

To put `tbhbdev` on PATH, which the `alpha` harness and git hooks need, install it as a uv tool:

```sh
uv tool install git+https://github.com/tbhb-dev/tools
```

`uv tool upgrade tbhbdev` updates it. `tbhbdev --version` (or `-V`) prints `tbhbdev <version> (<identity>)`: the commit the install came from, `editable <path>` or `local <path>` for a directory install, or `unknown`.

## Tools

- `analytics` applies PostgreSQL migrations and checks the installed catalog contract.
- `secret` reads and manages 1Password items through the `tbhbdev.secrets` library.
- `host` manages agent hosts from the operator's Mac.
- `alpha` holds the interim agent tools from ascm's `tools/interim`. They may change or go away without notice.

## Config

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

`[gh_budget]` sets the agent `gh` budget that [GitHub polling](#github-polling) describes. Its values are whole numbers, and the ones shown are the defaults.

Each `[connect] server_hosts` entry must be a DNS label of at most 63 characters. Letters and digits make up the label, and a hyphen may appear only between them. A bad entry fails with its position in the list, not its value.

That's the operator's file. On each agent host, `tbhbdev host bootstrap` installs a copy that adds `[connect] endpoints`, computed from the tailnet as [Agent hosts](#agent-hosts) describes.

## Analytics

`tbhbdev analytics migrate` connects as the owner, serializes migrations with an advisory lock and records each version in its migration transaction. `--dry-run` lists pending migrations without changing the database. Ops must create an empty database and its owner login with `CREATEROLE` before migration.

`tbhbdev analytics contract check` separately compares the database catalog with the packaged contract and exits 1 on a difference. `migrate` alone doesn't check the contract. A deploy runs `tbhbdev analytics migrate` and then `tbhbdev analytics contract check`. A configuration mistake prints its message, such as `[analytics] password_file is required`. That message contains key names and never a value. Other failures print only redacted diagnostics.

Configure the connection through `[analytics]` in the user config. Set `password_file` to the owner password file and `loader_enabled` to `true` to enable loader access. The switch defaults to `false`.

The optional keys `reader_password_file`, `grafana_password_file`, and `loader_password_file` each point at one role's password file. `migrate` sets the password of a role that has a file. It creates a role that has no file with no password and leaves an existing role's password unchanged. It prints a line such as `role analytics_reader has no password (reader_password_file is not configured)` for each such role, with `--dry-run` too.

The `PGHOST`, `PGPORT`, `PGDATABASE`, and `PGUSER` environment variables override the matching connection settings. Analytics defaults to `localhost`, port 5432, database `analytics`, and user `analytics_owner`. These defaults form an exception to the deployment settings described in Config. The user must equal `owner_role`. Each password file must contain 32 characters drawn from `A-Z`, `a-z`, and `0-9` on its first line. The owner password also seeds the salt of every role password. Role settings `owner_role`, `loader_role`, `reader_role`, and `grafana_role` override the standard identifiers, and no two of them may be equal. Populate these files from 1Password and keep them under operator control.

Default tests use fake connections. The opt-in server test requires an empty disposable PostgreSQL 18 database. It reads the server from `ANALYTICS_TEST_HOST`, the database from `ANALYTICS_TEST_DATABASE`, and the owner password file from `ANALYTICS_TEST_PASSWORD_FILE`, and it skips when any is unset. It connects to that one database and ignores the `[analytics]` config and the `PG*` variables. Run `ANALYTICS_TEST_HOST=localhost ANALYTICS_TEST_DATABASE=analytics_test ANALYTICS_TEST_PASSWORD_FILE=owner_password uv run pytest tests/analytics_server.py -m analytics_server` to check lock contention, rollback, and an unchanged rerun. The test changes that database and leaves its schema for inspection.

Bazel never selects the server test.

## Secrets

Tools get secrets only from `tbhbdev.secrets`. It's the one module that talks to 1Password Connect or `op` at runtime, so a fallback for hosts off the tailnet can go there later. `secrets.read("vault/item/field")` returns one value, and also takes the `op://vault/item/field` form. `secrets.Store` covers item management: get, list, create, edit, delete, and move.

The library reads the Connect token from the current user's own store: the agent keychain on macOS, and on Linux the file `.config/tbhbdev/secrets/connect-token` under `$HOME` (`secrets.LINUX_TOKEN_FILE`). Linux always uses `$HOME` as the base, so configuration management can write the token without consulting `XDG_CONFIG_HOME`. The token needs a regular file with mode `0600` in a `0700` `secrets` directory. The current user must own both. The reader rejects symlinks and any group or other access. Configuration management is the only writer of the Linux token file. The library tries the config's `[connect] endpoints` in order, the list bootstrap writes into an agent host's copy. A connection error or a 5xx answer moves it to the next endpoint, while a 4xx answer stops it.

Tests set the `TBHBDEV_TEST_TOKEN` environment variable, and the library then uses its value in place of the keychain token. Production code leaves it unset.

`tbhbdev secret read REF` prints exactly one value, for `$(tbhbdev secret read ...)`. `get` prints the item's metadata and field labels, adding values with `--reveal`. `create` and `edit` read the fields as a JSON object of label to value from stdin or `--file`, never from the command line. `delete` asks for `--yes`.

## Agent hosts

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

## Alpha commands

`tbhbdev alpha` holds the interim agent tools that the tbhb-dev repositories used to carry in `tools/interim`:

- `identity` records which agent and model made each edit and writes the `Generated-by` trailers.
- `guard-op` requires the configured `[op] account` flag on shell calls and proposed writes in every directory. `PreToolUse` covers Claude `Bash` and `Write|Edit|MultiEdit|NotebookEdit`, Codex `Bash`/`shell`/`apply_patch` and Antigravity `run_command`/edit tools. The guard scans shell files, Markdown shell fences, Python command expressions, notebook code cells, and YAML command fields. It allows unrelated edits to existing offenders. `OP_ACCOUNT` alone doesn't pass the guard. The module docstring lists the allowed command forms that the scanner misses. Check the Codex `apply_patch` PreToolUse matcher live after install. Its behavior remains unverified against Codex's documentation.
- `guard-gh-pr` is a PreToolUse hook that blocks `gh pr create`, `gh pr edit` and `gh pr merge`.
- `state-guard` is a Claude Code hook that blocks a status claim with no evidence in a SendMessage call or a final reply, and a `git commit` that adds a sign-off or co-author trailer.
- `session-context` is a Claude Code SessionStart hook that reports the host, its role, the other hosts, and the local time.
- `guard-gh-poll` is a PreToolUse hook that blocks GitHub polling and unsetting the 1Password service account token's variable.
- `guard-scripts` is a PreToolUse hook that keeps scripts for the operator in `~/.local/libexec/tbhb/`. It reads the common ways an agent writes a file, so it misses the rest. It denies a script written anywhere else under the home directory, `/tmp` or `/private/tmp`, and any script in a drafts repository. Git work trees, this session's scratchpad and a run-scoped `$TMPDIR` pass. Name each file `YYYY-MM-DD-<owner>-<slug>[.vN].sh` or `YYYY-MM-DD-<owner>-<slug>[.vN].py`. The guard refuses a write to an existing file. Give each revision a new name. Both suffixes and any content starting with `#!` count as scripts. When the hook can read the content, a new `.py` file there must start with exactly `#!/usr/bin/env -S python3 -I`. Make it executable with `chmod 0755`. Known gaps include unknown content (such as `echo` output) and the executable bit, which the hook can't check at write time. Run logs go in `~/.local/state/tbhb/<same name>/`.
- `gh-shim` is the `gh` that agent sessions find first on PATH. It budgets their `gh` calls as a backstop.
- `agent-env` runs Codex or Antigravity with the `gh` shim first on PATH.
- `pr` checks `.github/PULL_REQUEST_DRAFT.md` and opens or updates the pull request from it.
- `review` drafts and checks a pull request review.
- `gh-app` requests a GitHub App installation token, reading the App's id and RSA key through Connect, and keeps it under `$XDG_STATE_HOME/gh-app/VAULT/APP`. The command serves a cached token with more than ten minutes left, even during a cooldown, and a cooldown refuses any other request. Only a 404 sends the installation lookup from the repository to its owner. The command syncs each file to disk before it replaces the old one.
- `repo` holds repository commands. `repo bootstrap` applies the standard repository settings.
- `merge` squash-merges an approved pull request.
- `loop` validates its user configuration and refuses live intake until adapters connect the runner to its sources. Its shadow runner accepts typed event and reconcile interfaces for simulation. The package includes pure state derivation and rules that produce typed shadow intents with action keys and spawn budgets. A SQLite store records receipts, action keys, runs, and escalation markers.
- `agent` coordinates runs with `exec`, `wait`, `status`, `log`, `diff`, `kill`, `clean` and `keep`. `clear-taint HOST --reason TEXT` records local host clearance. The package also includes the run request's schema and the repo argument rules.
- `install` installs the git hooks and the user-level harness hooks.

Run `tbhbdev alpha install` once on each host, from a clone of a tbhb-dev repository. It does six things:

- It installs that clone's git hooks into `$(git rev-parse --git-common-dir)/hooks`, which every worktree of the clone shares. prek runs pre-commit and pre-push, and three shims run `tbhbdev alpha identity` for prepare-commit-msg, commit-msg and post-commit. Without `tbhbdev` on PATH, the shims block a commit in a tbhb-dev repository and do nothing elsewhere.
- It merges the hooks into each harness's user-level config: `settings.json` in `$CLAUDE_CONFIG_DIR` or `.claude` in `$HOME` for Claude Code, `hooks.json` in `$CODEX_HOME` or `.codex` in `$HOME` for Codex, and `.gemini/config/hooks.json` in `$HOME` for Antigravity.
- It records Codex's trust for the hooks it wrote. Codex runs a hook only when `config.toml` in `$CODEX_HOME` holds a trusted hash for it, so install sets `trusted_hash` under `[hooks.state."<key>"]` for each hook of its own, which is the trust `/hooks` grants a person. Entries for other hooks are unchanged, and so is the rest of `config.toml`, comments included. A hook whose command changes loses its trust, since the hash covers the command. The hash for a group with no matcher (`SessionStart`, `SubagentStart`) leaves `matcher` out, which nobody has checked against Codex yet. A live check after merge confirms it. If Codex hashes `"matcher": null` or `""` instead, those two hooks show as untrusted again. When a hook of its own moves to a new position, install removes the entry for the old one. A new `config.toml` is readable by the user only, an existing one keeps its mode, and one that isn't valid TOML is reported and left alone. The `SSH_AUTH_SOCK` setting below edits the same `config.toml` right after, so one run applies both, trust first, and each keeps the other's keys.
- When an installed plugin supplies `claude-private.md` or `mise-private.toml`, it writes them to `.claude/CLAUDE.private.md` and `.config/mise/conf.d/private.toml` in `$HOME`, readable by the user only. When no plugin supplies a file, it skips that file.
- It sets `SSH_AUTH_SOCK` to the empty string for every agent command, in the `env` object of Claude Code's `settings.json` and in `shell_environment_policy.set` of Codex's `config.toml`, so an agent's command doesn't connect to the 1Password SSH agent and prompt the operator. Host provisioning sets the variable for the account an agent runs as, but gives the operator's own account no harness environment, so `install` is the only thing that sets it there. It keeps every other key of both files, and the comments and layout of `config.toml`.
- It writes the `gh` shim, an executable `gh` in `$XDG_DATA_HOME/tbhbdev/agent-bin`, where `XDG_DATA_HOME` defaults to `.local/share` in the home directory.

It never prompts, runs `op` or needs the 1Password app, so configuration management can run it unattended, and a second run doesn't change anything. Each hook command it writes ends in `# tbhbdev alpha install`. A rerun replaces only those entries, and keeps everything else in place. Before it changes a config, `config.toml` included, it saves the old one beside it as `<name>.tbhbdev-backup.<time>`. `--dry-run` prints the changes without writing them, and `--harness claude|codex|antigravity` limits the merge to one harness.

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

These user-level hooks run in every repository. Before acting, the identity, pull request, approval guard, and install hooks check that `origin` names a tbhb-dev repository on GitHub. Outside a repository, the identity record hook still writes the session's record to the user's store under `~/.local/state/tbhbdev/tbhb-alpha`. Both `state-guard` and `session-context` run regardless of origin. At SessionStart, Claude Code runs `tbhbdev alpha install --hook`, which puts the `gh` shim first on PATH for the session's commands and reinstalls the git hooks of the worktree the session starts in. Its PostModelSwitch hook records a `/model` switch, so a commit in the next turn credits the new model.

The alpha commands write each worktree's state under `$(git rev-parse --git-dir)/tbhb-alpha`, and each worktree has its own pull request draft, so sessions in two worktrees of one clone don't overwrite each other.

### Loop configuration

`tbhbdev alpha loop` reads the optional `[loop]` table from the user config. Calling this command requires the settings shown below. An unreadable or invalid config file fails with its path. A missing table or key, an unknown key, or an invalid setting fails with the path and `[loop] key`. `--help` works without config.

```toml
[loop]
repositories = ["example-org/example-repo"]
approval_command = ["example-approve", "--quiet"]
store_path = "/example/state/loop.sqlite3"
reconcile_interval_minutes = 5
spawn_cap_total = 4
spawn_cap_per_host = 2
daily_spawn_cap = 20
hosts = ["example-host-a", "example-host-b"]
allow_claude_retry = false

[loop.rule_modes."example-org/example-repo"]
R0 = "shadow"
R1 = "shadow"
R2 = "shadow"
R4 = "shadow"
R9 = "shadow"
```

The settings have these constraints:

- `repositories` lists unique `owner/name` strings.
- `approval_command` lists the approval program followed by its arguments, preserving arguments with spaces. The loop doesn't store a token.
- `hosts` lists unique host strings. These three lists must contain at least one entry, and entries must be non-blank strings.
- `store_path` is a non-blank path string without a null byte. After expanding `~`, it must be absolute.
- The interval and caps are positive integers. Booleans and fractional values fail. The spawn caps cover the total and each host. `spawn_cap_per_host` must not exceed `spawn_cap_total`. The daily cap covers spawns per day.

`allow_claude_retry` is an optional boolean, defaulting to false. R8 retries on Codex unless this flag permits a timed-out or unknown Codex run to retry on Claude. It still needs configured host and harness capacity. `rule_modes` is an optional table of configured repositories, each containing R0 through R11 with values `off`, `shadow`, or `on`. Unlisted rules are off. The runner rejects every `on` mode with `LiveModeError` at startup. Live execution isn't implemented.

Even with valid settings, the command exits with status 1 and reports that the shadow runner exists but live intake isn't wired yet. It doesn't create a store or execute an approval or other action. The Python runner accepts `EventSource`, `ReconcileReader`, `PeerAgent`, `GitHubWriter`, and `Approval` protocols, with fakes in the tests. Intake supplies validated receipts and immutable observations from each delivered event and verified context. Reconcile reads supply fresh facts and typed evidence of completed effects.

`Runner.run` reconciles before consuming deliveries. R0 gates reconcile reads by rule mode, schedule, and quota reserve. The runner records each intent with state `shadow` and appends a JSON line with the receipt or sweep identifier, derived state, rule, key, kind, and outcome. Replayed deliveries and restarts use the same keys, while fresh observations supply PR state. R3 has no action key: each processed delivery or sweep logs the observed decline again, including repeated sweep identifiers. These lines describe observations and don't count as scored intents. The runner never calls an effect interface.

Each sweep compares prior intents with head-bound approvals, merges, reviewer runs, and typed effect observations. In Phase 0, R0 agrees on a sweep. Only R1, R2, R4, and R5 can reach `agree` from PR facts. R6 through R11 need explicit effect evidence to agree. A PR absent from the snapshot or superseded by a new head scores `no_change`. The runner rejects invalid effect results before writing. `score_shadow` records `agree`, `disagree`, or `no_change` in an immediate transaction and skips rows whose timestamps are ahead of the sweep. For each skipped non-final row, the runner logs `source` (the sweep key), `rule`, `action_key`, and `outcome: "skipped_future"` as one JSON line, while final comparisons remain immutable and don't generate skip logs. A later sweep can replace `no_change` with a final score.

`Runner.report()` reads one store snapshot and returns counts by rule and repository, plus the count of R9 items it would have written. Twenty final scores make a rule eligible for promotion review. Provisional `no_change` scores don't count. Each rule summary has `needs_review` set when any disagreement needs explanation before promotion.

Eligibility alone never permits promotion. Phase 0 rejects live modes regardless of score.

The store API takes the configured `store_path` with no default. `Store(path)` opens an existing database. Use `OpenMode.CREATE` for a new one. One connection uses write-ahead logging, full synchronization, a five-second busy timeout, and owner-only file permissions. The parent directory must be private and must not be a symlink. A sibling `<store_path>.lock` file excludes a second opener for the store's lifetime, including during a rebuild. `:memory:` supports isolated tests. Only the runner writes through this API. No table stores pull request state.

`record_intent(key, time)` claims a new key and returns false when a live key already exists. A live intent replaces an existing shadow row. Promotion resets its timestamps and result. `existing_keys()` excludes shadow rows. `shadow_keys()` supplies them separately. An action advances from `intent` to `started`, then `done` or `failed`. The store serializes transition reads and writes in one transaction. Invalid transitions and state/result pairs raise `TransitionError`. Done actions require `ok`. Failed actions require `error`, `timeout`, or `refused`. A shadow row can persist a runner-supplied `agree`, `disagree`, or `no_change` result without becoming live. The store doesn't compute shadow scores.

Receipts and identifiers reject values outside their patterns. Duplicate delivery ids return `duplicate` without replacing the first receipt: this means seen, not processed. `crashed_intents()` supplies keys that have no start. `unfinished_starts()` supplies started keys without a terminal result. Both need recovery reads before a retry. `record_escalation()` returns the marker and whether it created the row. `stopped_objects()` derives the rules' stopped objects from live escalation and stop keys.

`counts(object_key)` returns failure totals by key family across heads. Families keep the action kind, object, and any step or reason. Review totals include every verdict. The runner combines them with approvals from reads to fill the rules' `reviewer_runs_without_approval` input. Retry counts use recorded runs under `retry:<run>`. This count is a presence flag because action keys are unique. Shadow keys contribute neither failures nor run counts. The counts and existing keys feed the rules without changing their input types.

`StoreError` reports a missing, corrupt, or locked database. Schema version 3 adds shadow intent dimensions for reports. A mismatched version raises `SchemaVersionError` with code `SCHEMA_VERSION`, naming the found version and requiring a rebuild before any journal settings change. To rebuild it, stop the loop and open the store with `OpenMode.REBUILD`. Rebuilding discards the database and its sidecars. The lock remains acquired throughout the rebuild. `Recovery.KEYS_LOST` tells the runner that it no longer has previous action keys. The runner must reconcile before taking more work. Database errors fail the operation and close unreadable or damaged connections before another operation can use them.

The pure rules table requires a spawned reviewer's approval at the current green head and a complete diff read. Mechanical approval is undecided and defaults off in `RepositoryPolicy.mechanical_approval`. An explicit opt-in permits only an agent-opened PR with an exact configured template match. PRs opened by the loop process remain excluded. R1 skips loop-origin PRs that the loop can't approve. The caller must recompute `mechanical_template_matches`, `verification_passed`, and all other head-bound observations for the current head. Until the caller verifies the base repository, the rules skip the PR, including escalation. Pending signatures and pending CI have no rule or age timeout in this slice. R8 uses `stop:<run>` when capacity can't supply a retry target and reserves `retry:<run>` for a retry with a target. Escalation emits at most one intent per object per call and skips stopped objects.

### Agent configuration

`tbhbdev alpha agent` reads the optional `[agent]` table from the user config. Calling this command requires the table and every key shown below except `max_run_minutes`, `docker`, `expires`, the host’s `tbhbdev` path, and the `[agent.codex]` table. Unreadable config fails with its path. Missing or invalid settings fail with the path, the full table name and the key, as `[agent.sandbox] audit_dir is required`. Unknown keys fail the same way at any level. `exec` validates a headless run before it contacts the host. It checks the remote version, then claims the name before it streams the inputs. A tmux session runs the host supervisor. Install that supervisor separately. `wait`, `status`, `kill`, `clean`, `keep`, `log` and `diff` use the recorded target. The parser allows and ignores `host_tag` in `[agent]` because another command reads it.

Use the hidden host command as `tbhbdev alpha agent run-host NAME [REQUEST] --host HOST [--prompt FILE]`. Supply a versioned JSON request through `REQUEST`, stdin with `REQUEST` set to `-`, or `<name>.request.json` by leaving out the path. A prompt path defaults to the run's recorded prompt. Config must list the host. During setup, run-host fetches a bare mirror under `$XDG_STATE_HOME/tbhbdev/agent-mirrors/<owner>/<repo>.git` and creates a separate clone at `<account clone>/.worktrees/agent-<name>`. It reads credentials through the secrets library. It then starts the harness with the policy's explicit environment and records its exit. Run files and the index live under `tbhbdev/agent-runs` in the state directory. SIGHUP, SIGTERM, and SIGINT stop the harness group and refresh Codex auth before run-host writes `<name>.rc`.

The run clone has its own copy of the mirror's object store. The mirror never prunes objects. Run-host installs the identity and pre-commit hooks from its own templates without running code from the checkout. It finishes all git operations before starting the harness. Plain git in a run clone is unsafe for host processes.

Build runs can write their clone and temporary directory. They also get the configured cache roots. The account clone's git directory and the mirror stay outside those roots. The run clone's git config, hooks, and info directory are read-only. Commands that save an upstream, such as `git push -u`, can't write that config. Run-host refuses an account clone that points to another clone's git directory.

Claude runs load user settings and the run's generated settings. They ignore project settings and use only server configuration supplied for the run. The generated settings turn off hooks and web tools. Codex marks each run clone as untrusted, which skips its project configuration.

Run-host records phase `unknown` when it can't prove the group stopped. It keeps the harness exit status and appends cleanup messages to the existing log.

```toml
[agent]
launch_wait_seconds = 120
agy_timeout = "900s"
kill_grace_seconds = 10
max_run_minutes = 120
sockets_dir = "/example/sockets"

[agent.sandbox]
write_roots = [".cache", ".local/share/mise", ".local/state/mise"]
network_domains = ["api.github.com"]
# host_path = ["/opt/agent-host/bin"]
# codex_network_proxy_opt_out = "named reason for this host"
# agy_unsandboxed_opt_out = "named reason for this host"
# codex_unsandboxed_opt_out = "named reason for this host"
audit_dir = ".local/state/example-audit"
denied_home_paths = [".claude"]

[agent.sandbox.path]
linux = [".local/bin", "/usr/local/bin", "/usr/bin", "/bin"]
macos = [".local/bin", "/usr/bin", "/bin", "/opt/homebrew/bin"]

[agent.sandbox.agy]
review_read = [".config/example-gh", ".config/mise"]
build_read = [".config/example-tools", ".local/share/mise/installs"]
build_write = [".local/state/mise", ".local/share/mise"]

[agent.codex]
web_search = { build = "live", review = "live" }

[agent.credentials.agent]
gh = "example-vault/example-gh/token"
docker = "example-vault/example-docker/config"
reviews_app_id = "example-vault/example-app/app_id"
reviews_app_client_id = "example-vault/example-app/client_id"
reviews_app_pem = "example-vault/example-app/private_key"
signing = "example-vault/example-signing/private_key"

[agent.claude]
default_profile = "primary"

[agent.claude.profiles.primary]
token = "example/primary/token"

[agent.claude.profiles.secondary]
token = "example/secondary/token"
expires = "example/secondary/expires"

[[agent.host]]
name = "example-linux"
os = "linux"
shell = "bash"
account = "agent"
unsandboxed = false
accounts = ["agent"]
proven_harnesses = ["codex"]
```

Run-host refuses write roots that overlap its run directory, installation, host search directories, harness, or script interpreter. Narrow broad roots such as `~/.local` before running. It follows symlinks for each harness, interpreter, and `env` target. Every link, directory, and final file must belong to root or the run's account. None may grant group or world write access. Account-owned parts must stay outside every write root.

On macOS, Homebrew's directories grant group write access to `admin`, so run-host refuses a harness or interpreter that resolves through Homebrew. Install it elsewhere, such as `~/.local/bin`, or use system tools in `/usr/bin` and `/bin`.

Host tools and the harness parent use a fixed search path, with optional absolute directories from `host_path`. Run-host finds `git`, `ssh`, and `openssl`, plus `sysctl`, `ps`, and `security` on macOS, in `/usr/bin`, `/bin`, `/usr/sbin`, and `/sbin` before any Homebrew directory. Tool subprocesses use the rich sandbox PATH. Run-host protects the install directories behind mise shims on either PATH. `host_tools.resolve` refuses executables reached through `mise/shims`. Set `host_path` to the versioned install directories. Each upgrade changes those directory names.

Run-host copies a standalone Codex release to `<XDG_DATA_HOME or ~/.local/share>/tbhbdev/codex/<release dir name>/` before building its policy. It compares every regular file's `sha256` digest and mode with the source. It checks the full directory contents and refuses links outside the release. Run-host reuses a matching copy. It moves a changed copy aside with a timestamp. Before renaming a replacement into place, it checks the temporary sibling. Directories use mode 0755. Files keep their source modes. The copy stays outside every write root. The account's `.codex` directory stays fully denied. Run-host requires a standalone Codex install and refuses other installs.

Codex requires network proxy support (0.164.0 or later). Its proxy uses limited mode and denies Unix sockets. Only the exact hosts in `network_domains` have access, and the default list is empty. Claude builds use the same domain list and keep only the existing signing socket grant. Linux profiles resolve denied paths through symbolic links, keep existing paths, and remove duplicates before passing them to a harness. These paths include the Windows Subsystem for Linux, Docker, and user runtime sockets. A named `codex_network_proxy_opt_out` disables the proxy. Policy previews accept named opt-outs for agy or Codex full access. Run-host refuses agy. Authorized Codex full-access builds require the request, host, and config gates. Run-host announces full access with the authorization reference and appends a durable host taint marker before preparation. Tainted builds report `untrusted-host`. Run-host refuses tainted reviews. Reviews require reviews App references and refuse token request failures. A Codex network proxy opt-out leaves filesystem isolation in place. Run-host logs that exception on affected runs.

Run-host builds the harness command and adds the brief header. The driver sends the raw brief, an optional `<name>.schema.json`, and `--host` with a configured host name. Codex receives the schema path. Claude receives its contents. An unset, empty or relative `XDG_STATE_HOME` uses the home directory default. Finished runs keep their completion and log files when a rerun fails. Protected paths include shell startup files, Git and tmux config files, and socket parent directories below home.

For `[agent.sandbox]`, macOS hosts list `DARWIN_USER_TEMP_DIR` and `DARWIN_USER_CACHE_DIR` in `write_roots` as absolute paths. Linux hosts can list `/tmp`. The private `CODEX_HOME` stays outside these roots at `<state>/tbhbdev/agent-codex/<name>`, with mode 0700. Run-host removes it in `finish` after auth write-back. State defaults to `~/.local/state`.

Live Codex probes copy the account's auth through `credentials.auth_copy`, then use the production refresh write-back and removal path. Each probe asserts that its test directory contains no `auth.json` when it finishes. Each probe plants hooks only under the scratch run's home.

Run every opt-in live test from a clean checkout with this command, outside an agent sandbox. The state directory keeps the test root outside the system temp directories granted for writing.

```sh
TBHBDEV_SANDBOX_LIVE=1 uv run pytest --basetemp "$HOME/.local/state/tbhbdev/sandbox-live" tests/test_alpha_agent_sandbox_live.py tests/test_alpha_agent_sandbox_live_host.py tests/test_alpha_agent_git_parent.py tests/test_alpha_agent_git_commit_live.py
```

During the build probe, the installed identity hook stamps a commit with a `Generated-by` trailer. Both profiles attempt writes to six protected Git children, then compare each path with its host snapshot.

The settings have these constraints:

- The connection settings default to `ssh_connect_timeout = 15`, `ssh_keepalive_seconds = 30` and `ssh_keepalive_count_max = 3`. Each is a positive integer. Codex's `web_search` also has a default. The `[agent.codex]` table and each of its keys are optional, and each profile, `build` and `review`, defaults to `live`. A key can narrow it to `cached` or `disabled`.
- `launch_wait_seconds`, `kill_grace_seconds` and `max_run_minutes` are positive integers. `agy_timeout` is bare seconds or a duration ending in `s`, `m` or `h`, up to 7 days.
- Path settings are non-blank strings without a null byte or a `..` part. The sandbox lists must be non-empty.
- Every credential and profile `token` is a `vault/item/field` reference, and each part starts with a letter or digit, then continues with letters, digits, dots, `_` or `-`. `[agent.credentials.<account>]` takes one table for each account, and `docker` is optional.
- Only `signing` also accepts a field label with internal spaces and `?ssh-format=openssh`, which converts the private key to OpenSSH format in memory.
- `default_profile` names one of the profiles, matching without regard to case, so `Primary` selects `primary`. Profile names that differ only in case fail validation. Display uses the name as written.
- `[[agent.host]]` lists at least one host. Its `name` is lower-case, dot-separated labels, `os` is `linux` or `macos`, `shell` is `bash` or `zsh`, `account` must be one of `accounts`, and host names are unique. Every name in `accounts` needs an `[agent.credentials.<name>]` table. `proven_harnesses` is a typed list of harness names. Add a harness only after a passing live run on that host. Remove it when upgrading that harness. Run-host refuses Claude while separate tool paths for Claude (H3) and the Claude credential decision remain open.

The run request is a versioned JSON object. Version 2 lists every run field, including the required boolean `tainted_host`, and no credential. Readers refuse version 1 and requests missing `tainted_host`. A reader refuses unknown versions, unknown or missing fields, and any `unsandboxed` request unless the harness is Codex, the profile is `build`, the host's `unsandboxed` is true and the authorization has the form `owner/repo#N` or `operator-chat-YYYY-MM-DD`. The repo argument takes the form `org/repo` or `<git host>/<org>/<repo>`, with `github.com` as the default git host, and resolves to `Code/<git host>/<org>/<repo>` under the home directory. A repo name can't be `.` or `..` or end in `.git`.

### Agent isolation limits

Run-host runs git in a run clone only during `clone_run` and `create`, before launch. After the harness starts, run-host never runs git in that clone. An operator who runs `git add -A` or `git stash` in a run clone can run a nested repository's clean filter. The port never runs those commands in a run clone.

Run-host doesn't set upstream tracking before launch. It checks out a new build branch from a commit. Because `.git/config` stays read-only to the run, `git switch -c x origin/main` and `git push -u` can't record tracking. Use `git switch --no-track -c x origin/main` and push with an explicit destination branch, such as `git push origin HEAD:refs/heads/x`.

An `unsandboxed` run can read the account's 1Password Connect token and every secret it can read. It can write anything the account can. The host marker at `/var/tmp/tbhbdev-agent-taint/<account>.jsonl` is only a hint. The marked account can delete it. An account listed on any `unsandboxed = true` host can't run reviews on any host. A child can leave the process group and keep running after the run ends. Run-host refuses Claude while separate tool paths for Claude (H3) and the Claude credential decision remain open.
Every run's sandbox policy denies writes to the marker directory.

- A run can read anything that its deny list doesn't cover, including other runs' temp directories.
- Build runs have access to the account's `gh` token and Docker config.
- A run's tools can read its private Codex auth file.
- The agent can write the final answer file.
- Run-host sets Codex's `web_search` mode and removes the private `CODEX_HOME` at finish. It has no separate web search store or cleanup for any state that Codex or its provider keeps outside that home.

The coordinator traced every git process that the Codex parent starts, with every Git plant from the regression tests present. With codex-cli 0.166.0 on macOS and 0.164.0 on Linux, each session started 12 git processes before any tool call: `rev-parse HEAD` (3), `rev-parse --git-dir` (2), `rev-parse --abbrev-ref HEAD` (2), `remote get-url origin` (2), `remote -v` (1), `config --null --get core.fsmonitor` (1) and `status --porcelain` (1). Each one used Codex's own `-c safe.bareRepository=explicit -c core.hooksPath=/dev/null -c core.fsmonitor=false`. None started after a tool call. Version 0.164.0 also cloned its plugin repository into `CODEX_HOME`'s temp directory and ran `git maintenance run --auto` there. Re-check these observations on every Codex upgrade.

On Linux, a `.git` file can prevent Codex from starting a sandbox. [Codex 0.164.0 resolves Git pointer targets](https://github.com/openai/codex/blob/rust-v0.164.0/codex-rs/protocol/src/permissions.rs#L2371) and [adds protected child mounts to each writable root](https://github.com/openai/codex/blob/rust-v0.164.0/codex-rs/linux-sandbox/src/bwrap.rs#L689). The build policy grants writes to `.git` so tools can commit, but a Git pointer file can't hold those child targets. The live probe accepts this refusal only when the same shell command succeeds without the plant, the parent's Git calls leave the plant's marker absent, and a direct sandbox invocation returns a `bwrap` `Not a directory` error for that `.git` path. A model's message alone doesn't prove a refusal.

The terminal probe records a missing host terminal separately from a refused ioctl. [Codex's Linux sandbox mounts a private device tree](https://github.com/openai/codex/blob/rust-v0.164.0/codex-rs/linux-sandbox/src/bwrap.rs#L504), so opening the recorded outer terminal under `/dev/pts` can fail before an ioctl. The probe accepts a missing device on Linux only after an input control succeeds or the kernel refuses an ioctl. A successful ioctl or injected input fails either way.

Clear the host after checking it, then launch a run with a local prompt file:

```sh
tbhbdev alpha agent clear-taint example-linux --reason "Host rebuilt"
tbhbdev alpha agent exec --host example-linux --repo example-org/example-repo --model example-model --effort high --profile build example brief.md
tbhbdev alpha agent status example
tbhbdev alpha agent wait example
```

Reviews accept `--ref` and resolve it before launch. `--schema` streams a JSON schema beside the prompt. Claude requires `--reason` and accepts `--claude-profile`. Use a `gemini-*` model for agy, or pass `--allow-non-gemini`. `--ops` selects the host's configured `ops` account.

The optional `[[agent.host]] tbhbdev` key names an absolute executable path on that host. Its default is `$HOME/.local/bin/tbhbdev`, using the remote account’s home. Local `ssh` and `tmux` resolve against a fixed system PATH. Remote scripts use that PATH, add the standard package directories on macOS, and skip login shell startup.

Each coordinator writes a per-host taint record to `$XDG_STATE_HOME/tbhbdev/agent-runs/taint/<host>.jsonl` in this phase. A mark from any account taints the entire host. The account remains in the record as information. Missing or empty records mean `unknown`. The coordinator refuses review runs on unknown or tainted hosts before SSH. Build runs can proceed, labelled `untrusted-host`. `status` shows `host=unknown` or `host=tainted` when applicable.

Before a `--unsandboxed` launch, the coordinator appends a durable record using local run and authorization values. Failure to append refuses the launch. The last appended record decides trust, regardless of its timestamp. Only `clear-taint HOST --reason TEXT` clears local taint and prints the new state. A host needs an initial clearance before its first review. If the last line is incomplete, clearance moves the incomplete file aside in the same directory and starts a new file.

Run-host appends `--unsandboxed` marks to `/var/tmp/tbhbdev-agent-taint/<account>.jsonl` on the run host. A coordinator whose local record is clean reads at most 64 KiB of that marker before preflight. Any line records local taint and refuses review. A missing marker supplies no evidence of cleanliness. Read errors also refuse review. Clearance changes only this coordinator's record. An existing host marker marks it again. Operators must rebuild or check the host and address its marker before clearing the local record.

The coordinator stores metadata under `$XDG_STATE_HOME/tbhbdev/agent-runs/` on the coordinator. The remote directory uses the same location under the run account, with `.local/state` under home as the default state directory. The transfer claims `<name>.owner` with `set -C` before extracting inputs. It refuses an existing session or any `<name>.*` file with exit status 73. A collision never kills the existing session.

The tmux socket is `-S <sockets_dir>/agents`. The driver creates the configured directory with mode 0700 and refuses a symlink, another owner, or any other mode. Sandbox policies deny reads there. Whether each sandbox blocks a Unix socket connection needs the shadow test.

`wait` prefers the final answer and returns the run's exit status. Its poll interval defaults to 30 seconds (`TBHB_AGENT_WAIT_POLL`). Consecutive connection failures stop after 240 seconds (`TBHB_AGENT_WAIT_UNREACHABLE`) with exit status 75. Exit codes 1 and 75 overlap with harness exit codes. Read the first result line to distinguish an unknown or unreachable run from a harness result. Agent text follows that line.

Each claim writes a random run ID to the local metadata and the remote `<name>.owner` file before extracting inputs. Later commands check that ID before they act. A mismatch or missing owner archives local metadata and reports `not owner`. After a transfer error, the driver retains metadata only when a remote owner check succeeds. The driver sends the raw brief and starts run-host with the configured `--host`. Every tmux call uses `-f /dev/null`, and session creation passes an absolute executable with separate arguments.

`kill` closes the tmux session to notify the supervisor with SIGHUP. The supervisor stops its children before it records the return code. The coordinator writes 137 only after the session closes and the grace period elapses without a return code. It also writes `<name>.rc.fallback`, which makes `clean` require `--force` because processes might still run.

The host keeps a bare mirror at `$XDG_STATE_HOME/tbhbdev/agent-mirrors/<git host>/<owner>/<repo>.git`. An empty or unset `XDG_STATE_HOME` uses `~/.local/state`. Only host git touches the mirror, under its `agent-mirror.lock`. Before launch, the supervisor makes a separate run clone with `git clone --shared` at `<account clone>/.worktrees/agent-<name>`. That clone has its own `.git` directory. The sandbox denies writes to `.git/config`, `.git/hooks` and `.git/info`. After launch, host processes never run git in the run clone or trust its git state.

```text
Don't run git inside a run clone: it is agent-written. Use `tbhbdev alpha agent log|diff NAME`.
```

`log NAME` shows up to ten commits by default. `log -n 5 NAME` changes that count. `diff NAME` shows changes from the default branch's merge base to the pushed branch. Both commands fetch the origin's default branch and `agent/<name>` into the mirror, then inspect those refs with external diff, text conversion, and color turned off. They show pushed work only. Neither `wait` nor `status` needs the mirror. The mirror never prunes, so disk use grows with history until a pruning follow-up.

Cleanup refuses an open session. Without `--force`, it checks `refs/heads/agent/<name>` on origin through the mirror. A missing branch, missing mirror, or failed query refuses cleanup. A present branch reports `branch exists on origin`. This check establishes presence only, so the run clone may still hold work that the agent hasn't pushed. Cleanup moves the run clone and the per-run files into dated trash. It refuses a symlink in place of the run clone. Retries skip completed moves, and removing all files for the run permits name reuse. `keep` succeeds when the marker already exists.

Ref resolution runs outside the clone against the SSH URL from the repository argument. Remote Git uses an empty environment and explicit overrides for hooks, file monitoring, SSH, credentials, prompts, external protocols, paging, submodules, and automatic maintenance. It disables lazy fetching. Remote scripts use the C locale. Run file reads refuse symlinks, bound byte counts, check status fields, replace invalid UTF-8, and remove terminal control characters. Polls wait at least half a second between connections.

#### Risk

The taint record is per coordinator, and the host marker is a hint that the marked account can delete.

The driver refuses reviews on a host with no record until `clear-taint`.

The driver's mirror receives agent-pushed objects.

### Secret scan

`tbhbdev alpha pr create` and `pr edit` scan the final title and body, trailers included, and `tbhbdev alpha merge` scans the squash message, with `gitleaks stdin --redact` before `gh` runs. The scan uses the host's private config, `.config/gitleaks/private.toml` in the home directory, which the host's provisioning tooling installs and which extends the repository's `.gitleaks.toml`. It fails closed. The command stops on a private config that's missing, unreadable or lacks the `tbhb-private-canary` rule. A missing `.gitleaks.toml`, a gitleaks failure, or any finding stops it too. Findings appear by rule ID and line only. `pr check` and the draft hook don't scan.

The scan uses the visibility of the repository the text goes to, not the working directory's. `pr edit` and `merge` take that repository from the pull request's own `url` and read `gh repo view <owner/name>` once. `pr create` has no pull request yet, so it reads `gh repo view` once for the base repository, which is the one `gh pr create` resolves. A `url` that isn't `https://github.com/<owner>/<name>/pull/<number>`, or a base repository without a parseable `nameWithOwner`, is an unresolved target, and then edit and merge don't read the visibility. In a repository that `gh` reports as `PRIVATE`, the scan skips the private config's name rules: the rule IDs `private-repo`, `tailnet-host`, `tailnet-stem`, `tailnet-name`, `tailnet-suffix`, `local-name`, `home-path` and `tool-dir`, each with its trailing `-<digits>`. This matches the rules' gates, which apply the private config only to a repository that sets `--@tbhb_rules//lint:gitleaks_private_config`. The scan runs under a temporary copy of the private config without those rules, in a 0700 directory as a 0600 file, removed afterwards. Every other rule still applies: the `op-*` rules, `tailnet-ipv4`, `tailnet-ipv6`, `private-ipv4`, `mac-address`, `endpoint` and `keychain`, the canary, `.gitleaks.toml` and gitleaks' defaults. `PUBLIC`, `INTERNAL`, any other value, an empty value, a failed read or an unresolved target gets the full private config.

### No self-approval

A session can't approve a pull request that it, or one of its subagents, opened. The rule compares root session hashes, the `session=` value in the trailers, which a subagent shares with its root.

- `tbhbdev alpha pr create` writes `Opened-by: LLM …` as the first line of the body's trailer paragraph. It holds the opener's root identity, with no `subagent=`. `pr edit` copies the line from the live body unchanged, `pr check` rejects a draft that contains one, and `tbhbdev alpha merge` drops it from the squash commit. When the tool can't find an agent's root, as for an Antigravity child with no brain directory, it doesn't write the line. It also skips the line and the ledger entry, with a warning, when the root's value fails the trailer check, such as one without `model=`. The merge refuses a body with an `Opened-by` line anywhere outside the final trailer paragraph, fenced code included.
- Once `gh` opens the pull request, `pr create` adds `owner/repo#N` to the opener's ledger, `opened/<root hex>.json` in `$XDG_STATE_HOME/tbhbdev/tbhb-alpha`, which defaults to `~/.local/state/tbhbdev/tbhb-alpha`.
- `guard-gh-pr` denies `gh pr review N --approve`, a `gh api` POST to `repos/O/R/pulls/N/reviews` with `event=APPROVE`, and `tbhb-approve REPO N SHA` when the caller's ledger lists that pull request. Without `-R`, the repository comes from a `GH_REPO=` prefix, else the origin of the hook's directory. After a `cd`, `pushd`, `popd` or `git -C` in the same command line, that origin is unknown. An unreadable ledger, a session it can't identify, or, for a session that opened anything, a pull request it can't name denies too. A session with no ledger file opened nothing and may approve.
- `tbhbdev alpha pr close N --reason TEXT [--delete-branch] [--dry-run]` closes an open pull request, and `tbhbdev alpha issue close N --reason completed|not-planned [--comment TEXT] [--dry-run]` closes an open issue. A blank `--reason` or `--comment` is a usage error. Both act through the agent's own `gh` and leave a final comment that holds the reason and the agent's `Generated-by` trailer, and both refuse a caller that isn't an agent session. `pr close` refuses a pull request with no `Opened-by` line, and notes in its comment when the closer isn't the opener. With `--delete-branch`, it then deletes the head branch, unless the branch is on a fork, which it leaves alone and says so. Before it comments or closes, `--delete-branch` lists the open pull requests that target the head branch and refuses, naming them, when any exist or when `gh` can't list them. That policy is provisional until the alpha-review proposal settles open question 11.
- `tbhbdev alpha pr ready --pr N` marks pull request N ready for review through `gh pr ready`. It doesn't read a draft.
- `tbhbdev alpha pr backfill-opened OWNER` covers the pull requests opened before the ledger existed. It reads every pull request, open, closed and merged, in every repository of the owner, and adds each one to the ledger of every `session=` its `Generated-by`, `Assisted-by` and `Opened-by` lines name. Run it once on each host. A rerun doesn't change anything. It warns when a listing reaches its `--limit`.

The guard reads only this host's ledger and sees only the commands it scans, so this isn't a security boundary.

### Review drafts

`tbhbdev alpha review draft N` reads the PR's head through `gh` and writes a skeleton to `$(git rev-parse --git-dir)/tbhb-alpha/review-N.md`. It prints the path and keeps an existing draft unchanged. `--force` refreshes only `commit_id`. It keeps the body and the other metadata. Each worktree and PR has its own file.

Pick `APPROVE`, `REQUEST_CHANGES`, or `COMMENT` in the draft's `event` field. The required frontmatter also holds `pr`, which must match N, and a 40-hex `commit_id`. Reviews have no H1 title. Fill Verdict and Verification, then remove the guidance comments and any unused optional sections. Findings holds blocking issues, one list item each with `path:line` and what must change. A request for changes needs findings. Suggestions holds non-blocking improvements.

`tbhbdev alpha review check N` checks the draft locally, including its frontmatter, section order, word limits, prose wrapping, references, trailers, and the repository's Markdown, spelling, and prose rules. It prints each problem as `path:line: problem` on stderr and exits 1 on failure or 0 on success. The check reads local files and runs local linters. Write plain prose, one line per paragraph, cite code as `path:line`, and leave trailers out.

### Repository settings

`tbhbdev alpha repo bootstrap [--dry-run] [--create (--private|--public)] ORG/REPO` brings a repository to the standard settings. It's idempotent: when a repository already matches, the command prints `up to date` and stops.

The settings aren't in this package. An installed plugin supplies the config file `repo-settings.toml`. The settings change only by a pull request in that plugin's repository. When no plugin supplies the file, the command fails before it calls GitHub, and the message says which file is missing. The config must have a `[defaults]` table. A `[repos."owner/name"]` table adds to the defaults for one repository, and its keys win. Repository names match without regard to case. A repository name may begin with a dot, as in `example-org/.github`. The names `.` and `..` fail, and so does an owner that begins with a dot. The command rejects any other character than letters, digits, `.`, `_` and `-`, in the command-line argument and in a config key alike.

```toml
[defaults]
allow_squash_merge = true
allow_merge_commit = false
allow_rebase_merge = false
squash_merge_commit_title = "PR_TITLE"
squash_merge_commit_message = "BLANK"
delete_branch_on_merge = true
has_issues = false
has_wiki = false
has_projects = false

[repos."example-org/example-repo"]
has_issues = true
```

Each key is a field of GitHub's update-a-repository endpoint, and only these keys are valid: `allow_squash_merge`, `allow_merge_commit`, `allow_rebase_merge`, `squash_merge_commit_title` (`PR_TITLE` or `COMMIT_OR_PR_TITLE`), `squash_merge_commit_message` (`PR_BODY`, `COMMIT_MESSAGES`, or `BLANK`), `delete_branch_on_merge`, `has_issues`, `has_wiki`, `has_projects`, `allow_auto_merge`, `allow_update_branch`, and `web_commit_signoff_required`. All but the two commit-message keys take `true` or `false`. Any other key fails validation with a message that gives the file and the key. That includes `visibility`, `private`, `archived`, `default_branch`, `name`, and the security settings. A config can't rename, archive, or expose a repository.

The command runs `gh api` with `GH_CONFIG_DIR` set to `.config/gh-agent` in the home directory. It removes `GH_TOKEN`, `GITHUB_TOKEN`, `GH_ENTERPRISE_TOKEN`, `GITHUB_ENTERPRISE_TOKEN`, and `GH_HOST` from the environment it gives `gh`, because `gh` prefers a token variable to its config. The agent's `gh` config is the only credential, and a missing login fails with a `gh` error instead of falling back to another one. It reads `GET /repos/{owner}/{repo}` and sends one `PATCH` of the keys that differ. Then it reads again. If a key still differs after the update, the command exits 1 and reports the key. GitHub ignores some fields on some plans and for some visibilities. When a squash commit-message key changes, the `PATCH` includes both of them, because GitHub requires the title with the message. When the config sets only one of them, the other goes in with the repository's current value. The verifying read compares only the keys the config sets. Rulesets are out of scope. The command never calls a ruleset, branch protection, collaborator, team, or webhook endpoint. Its only calls are `GET` and `PATCH` on `/repos/{owner}/{repo}` and `POST` on `/orgs/{org}/repos`.

- `--dry-run` reads only. It prints a `key: before -> after` line for each key that would change, then a count.
- `--create` needs exactly one of `--private` and `--public`. For an existing repository, a visibility that differs from the flag exits 1 before any write, and a matching one goes on to the settings. A missing repository gets `POST /orgs/{org}/repos` and then the settings. Without `--create`, it exits 1.
- Every run that changes something appends one JSON line to `$XDG_STATE_HOME/tbhbdev/repo-bootstrap.jsonl`, which defaults to `~/.local/state/tbhbdev/repo-bootstrap.jsonl`. The directory has mode 0700 and the file 0600. A line has only the fields `v` (always 1), `ts` (the time, such as `2026-11-09T13:30:05Z`), `repo`, `created`, `changed` (each key with its `[before, after]` values), and `verified`. Dry runs and up-to-date runs leave the log alone. The command also sets those modes on an existing log file and directory, not only on new ones. A repository the command created gets a line even when a later step fails: `created` is `true` and `verified` is `false`, and `changed` is empty when the failure came on the first read after the create. If a `gh` call fails and the log write fails too, the command reports the `gh` error. If the log write fails after a successful `PATCH`, the command exits 1 after the change went through.

Exit status 0 covers a finished update, an up-to-date repository, and a dry run. Exit status 1 covers a config, validation, `gh`, or verification failure, with one line on stderr. Exit status 2 covers a usage error.

### GitHub polling

Agents get GitHub CI and pull request state from awrel, which relays webhook events into a session: subscribe with the repository and the event type, such as `check_run` conclusion events. That's the way to watch GitHub state. Polling GitHub instead burns the account's REST quota, which every agent shares. A hook stops the common polls, and a shim budgets what remains. The budget is a backstop, not the way to watch GitHub state.

- `guard-gh-poll` denies `gh run watch`, `gh run list`, `gh pr checks --watch`, `gh api` or `gh query` with `--paginate`, and reads of the list endpoints `actions/runs`, `actions/workflows/W/runs`, `actions/jobs`, `check-runs` and `check-suites`. It also denies a `while`, `until` or `for` loop that runs `gh`, or runs both `sleep` and `pgrep`, such as the self-matching wait `while pgrep -f fetch.py; do sleep 5; done`. Single-object reads pass, such as `actions/runs/ID`, `pulls/N`, `commits/SHA/check-runs`, `actions/runs?head_sha=SHA` for one full commit hash, and `rate_limit`. It scans `bash -c`, `eval`, `ssh HOST COMMAND` and a here-document fed to a shell. Each deny names awrel. The same hook denies `unset` and `env -u` of the 1Password service account token's variable. A command keeps the environment the harness gave it.
- `gh-shim` budgets the `gh` calls that scripts make, which no hook sees. In an agent session it refuses a call when the root session has made `[gh_budget] hourly_limit` `gh` calls in the past hour, or when the account has fewer than `core_floor` core REST requests left. It checks that with `gh api rate_limit` at most once a minute. A refusal goes to stderr and names awrel and the budget. Otherwise, and always outside an agent session, it runs the real `gh` unchanged. The state is under `$XDG_STATE_HOME/tbhbdev/gh-budget`.

Claude Code gets the shim on PATH from the SessionStart hook, which adds an `export PATH` line to `$CLAUDE_ENV_FILE`. Codex and Antigravity have no setting that prepends to PATH, so start them as `tbhbdev alpha agent-env codex` or `tbhbdev alpha agent-env agy`, followed by the harness's own arguments. A process tree that doesn't inherit that PATH, or a `gh` run by its absolute path, skips the shim. Neither mechanism is a security boundary.

## Plugins

Another installed package can add commands and config through an entry point in the `tbhbdev.plugins` group. The entry point resolves to a module or other object with either or both of:

- `TOOLS: dict[str, Tool]`, extra commands. A name that's already taken is an error, never a silent override.
- `config(name: str) -> str | None`, the text of a named config file, or `None`. The package doesn't include a config file of its own, so there's no default for a plugin's file to replace. The built-in tools read deployment values from the user config alone, never from a plugin.

Plugins load on first use. When one fails to import, `tbhbdev` names it and the error on stderr, and the built-in commands keep working. To run `tbhbdev` with a plugin, install both in one environment:

```sh
uvx --from git+https://github.com/tbhb-dev/tools --with <plugin package> tbhbdev <tool> [args...]
```

## Layout

- `src/tbhbdev/` is the package. Each tool is a module there, registered in `cli.py`.
- `src/tbhbdev/alpha/` is the `alpha` package, one module for each alpha command.
- `tests/` holds the tests, one `test_<tool>.py` for each tool and `test_alpha_<command>.py` for each alpha command.

## Checks

Bazel runs every gate from tbhb-dev/rules: `bazelisk test //...`. See [AGENTS.md](AGENTS.md) for the setup and the workflow.

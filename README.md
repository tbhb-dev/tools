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

`uv tool upgrade tbhbdev` updates it.

## Tools

- `secret` reads and manages 1Password items through the `tbhbdev.secrets` library.
- `host` manages agent hosts from the operator's Mac.
- `alpha` holds the interim agent tools from ascm's `tools/interim`. They may change or go away without notice.

## Config

The values of one deployment come from the user config at `$XDG_CONFIG_HOME/tbhbdev/config.toml`, where `XDG_CONFIG_HOME` defaults to `.config` in the home directory. The package has no defaults for them, and no environment variable or plugin overrides them. When the file or a required key is missing, the command fails, and its error gives the file's path and the `[table] key`. Optional settings include the `[workspace]` and `[loop]` tables and `[gitleaks] session_sockets`. Calling `alpha loop` requires its settings in `[loop]`. `tbhbdev` reads the file only when a command first needs a value, so `--help` works without it.

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
```

Each `[connect] server_hosts` entry must be a DNS label of at most 63 characters. Letters and digits make up the label, and a hyphen may appear only between them. A bad entry fails with its position in the list, not its value.

That's the operator's file. On each agent host, `tbhbdev host bootstrap` installs a copy that adds `[connect] endpoints`, computed from the tailnet as [Agent hosts](#agent-hosts) describes.

## Secrets

Tools get secrets only from `tbhbdev.secrets`. It's the one module that talks to 1Password Connect or `op` at runtime, so a fallback for hosts off the tailnet can go there later. `secrets.read("vault/item/field")` returns one value, and also takes the `op://vault/item/field` form. `secrets.Store` covers item management: get, list, create, edit, delete, and move.

The library reads the Connect token from the current user's keychain: the agent keychain on macOS, and `secret-tool` on Linux. It tries the config's `[connect] endpoints` in order, the list bootstrap writes into an agent host's copy. A connection error or a 5xx answer moves it to the next endpoint, while a 4xx answer stops it.

Tests set the `TBHBDEV_TEST_TOKEN` environment variable, and the library then uses its value in place of the keychain token. Production code leaves it unset.

`tbhbdev secret read REF` prints exactly one value, for `$(tbhbdev secret read ...)`. `get` prints the item's metadata and field labels, adding values with `--reveal`. `create` and `edit` read the fields as a JSON object of label to value from stdin or `--file`, never from the command line. `delete` asks for `--yes`.

## Agent hosts

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

Bootstrap creates the group `devel` with group ID 33835 and the user `agent` with user ID 24368, with `devel` as its primary group, no password login, and a home of mode 0700. On macOS it uses `dscl .` and hides the user from the login window. On Linux it uses `groupadd` and `useradd` and locks the password. It never changes an existing account, and an account with other IDs stops that host with an error, as does a user or group ID that another name already uses. Bootstrap runs its root steps with `sudo -n`. When sudo needs a password, it runs them through `ssh -t`, so sudo asks once per host.

The user config's `[workspace]` table names the workspace with `[workspace] root`, a path below the operator's home, and `repos`, a list of `owner/name`. With one, bootstrap:

- lets `devel` traverse each directory from the home down to the root
- gives `devel` inheritable read, write, and create access on the root and everything below it
- adds only missing entries, and reapplies the root's entries below the root only when the root lacked them or `--repair-acl` asks
- ends with a read and write probe as the agent

Bootstrap and upgrade skip the workspace when the config has no `[workspace]`.

## Alpha commands

`tbhbdev alpha` holds the interim agent tools that the tbhb-dev repositories used to carry in `tools/interim`:

- `identity` records which agent and model made each edit and writes the `Generated-by` trailers.
- `guard-gh-pr` is a PreToolUse hook that blocks `gh pr create`, `gh pr edit` and `gh pr merge`.
- `state-guard` is a Claude Code hook that blocks a status claim with no evidence in a SendMessage call or a final reply, and a `git commit` that adds a sign-off or co-author trailer.
- `session-context` is a Claude Code SessionStart hook that reports the host, its role, the other hosts, and the local time.
- `pr` checks `.github/PULL_REQUEST_DRAFT.md` and opens or updates the pull request from it.
- `merge` squash-merges an approved pull request.
- `loop` validates its user configuration and refuses to run until a later slice adds the shadow runner. Its pure functions derive the state of a pull request.
- `install` installs the git hooks and the user-level harness hooks.

Run `tbhbdev alpha install` once on each host, from a clone of a tbhb-dev repository. It does two things:

- It installs that clone's git hooks into `$(git rev-parse --git-common-dir)/hooks`, which every worktree of the clone shares. prek runs pre-commit and pre-push, and three shims run `tbhbdev alpha identity` for prepare-commit-msg, commit-msg and post-commit. Without `tbhbdev` on PATH, the shims block a commit in a tbhb-dev repository and do nothing elsewhere.
- It merges the hooks into each harness's user-level config: `settings.json` in `$CLAUDE_CONFIG_DIR` or `.claude` in `$HOME` for Claude Code, `hooks.json` in `$CODEX_HOME` or `.codex` in `$HOME` for Codex, and `.gemini/config/hooks.json` in `$HOME` for Antigravity.

Each hook command it writes ends in `# tbhbdev alpha install`. A rerun replaces only those entries, and keeps everything else in place. Before it changes a config, it saves the old one beside it as `<name>.tbhbdev-backup.<time>`. `--dry-run` prints the changes without writing them, and `--harness claude|codex|antigravity` limits the merge to one harness. Codex asks you to trust new hooks with `/hooks`.

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

These user-level hooks run in every repository. Before acting, the identity, pull request, approval guard, and install hooks check that `origin` names a tbhb-dev repository on GitHub. Outside a repository, the identity record hook still writes the session's record to the user's store under `~/.local/state/tbhbdev/tbhb-alpha`. Both `state-guard` and `session-context` run regardless of origin. At SessionStart, Claude Code runs `tbhbdev alpha install --hook`, reinstalling the git hooks of the worktree the session starts in. Its PostModelSwitch hook records a `/model` switch, so a commit in the next turn credits the new model.

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
```

The settings have these constraints:

- `repositories` lists unique `owner/name` strings.
- `approval_command` lists the approval program followed by its arguments, preserving arguments with spaces. The loop doesn't store a token.
- `hosts` lists unique host strings. These three lists must contain at least one entry, and entries must be non-blank strings.
- `store_path` is a non-blank path string without a null byte. After expanding `~`, it must be absolute.
- The interval and caps are positive integers. Booleans and fractional values fail. The spawn caps cover the total and each host. `spawn_cap_per_host` must not exceed `spawn_cap_total`. The daily cap covers spawns per day.

This slice only validates configuration. Even with valid settings, the command exits with status 1 and reports that the shadow runner isn't available. It doesn't create a store or execute an approval or other action.

### Secret scan

`tbhbdev alpha pr create` and `pr edit` scan the final title and body, trailers included, and `tbhbdev alpha merge` scans the squash message, with `gitleaks stdin --redact` before `gh` runs. The scan uses the host's private config, `.config/gitleaks/private.toml` in the home directory, which `host upgrade` installs and which extends the repository's `.gitleaks.toml`. It fails closed: a missing or unreadable private config, one without the `tbhb-private-canary` rule, a missing `.gitleaks.toml`, a finding or a gitleaks error stops the command. A finding is reported by rule ID and line only. `pr check` and the draft hook don't scan.

### No self-approval

A session can't approve a pull request that it, or one of its subagents, opened. The rule compares root session hashes, the `session=` value in the trailers, which a subagent shares with its root.

- `tbhbdev alpha pr create` writes `Opened-by: LLM …` as the first line of the body's trailer paragraph. It holds the opener's root identity, with no `subagent=`. `pr edit` copies the line from the live body unchanged, `pr check` rejects a draft that contains one, and `tbhbdev alpha merge` drops it from the squash commit. When the tool can't find an agent's root, as for an Antigravity child with no brain directory, it doesn't write the line. It also skips the line and the ledger entry, with a warning, when the root's value fails the trailer check, such as one without `model=`. The merge refuses a body with an `Opened-by` line anywhere outside the final trailer paragraph, fenced code included.
- Once `gh` opens the pull request, `pr create` adds `owner/repo#N` to the opener's ledger, `opened/<root hex>.json` in `$XDG_STATE_HOME/tbhbdev/tbhb-alpha`, which defaults to `~/.local/state/tbhbdev/tbhb-alpha`.
- `guard-gh-pr` denies `gh pr review N --approve`, a `gh api` POST to `repos/O/R/pulls/N/reviews` with `event=APPROVE`, and `tbhb-approve REPO N SHA` when the caller's ledger lists that pull request. Without `-R`, the repository comes from a `GH_REPO=` prefix, else the origin of the hook's directory. After a `cd`, `pushd`, `popd` or `git -C` in the same command line, that origin is unknown. An unreadable ledger, a session it can't identify, or, for a session that opened anything, a pull request it can't name denies too. A session with no ledger file opened nothing and may approve.
- `tbhbdev alpha pr close N --reason TEXT [--delete-branch] [--dry-run]` closes an open pull request, and `tbhbdev alpha issue close N --reason completed|not-planned [--comment TEXT] [--dry-run]` closes an open issue. A blank `--reason` or `--comment` is a usage error. Both act through the agent's own `gh` and leave a final comment that holds the reason and the agent's `Generated-by` trailer, and both refuse a caller that isn't an agent session. `pr close` refuses a pull request with no `Opened-by` line, and notes in its comment when the closer isn't the opener. With `--delete-branch`, it then deletes the head branch, unless the branch is on a fork, which it leaves alone and says so. Before it comments or closes, `--delete-branch` lists the open pull requests that target the head branch and refuses, naming them, when any exist or when `gh` can't list them. That policy is provisional until the alpha-review proposal settles open question 11.
- `tbhbdev alpha pr ready --pr N` marks pull request N ready for review through `gh pr ready`. It doesn't read a draft.
- `tbhbdev alpha pr backfill-opened OWNER` covers the pull requests opened before the ledger existed. It reads every pull request, open, closed and merged, in every repository of the owner, and adds each one to the ledger of every `session=` its `Generated-by`, `Assisted-by` and `Opened-by` lines name. Run it once on each host. A rerun doesn't change anything. It warns when a listing reaches its `--limit`.

The guard reads only this host's ledger and sees only the commands it scans, so this isn't a security boundary.

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

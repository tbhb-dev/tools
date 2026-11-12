---
description: Agent settings, run requests, and host preparation.
generated:
  - role: generated
    harness: codex
    model: openai:gpt-6
    at: "2026-11-12T09:46:11Z"
status: stable
tags: [alpha]
title: Agent configuration
type: Reference
---

# Agent configuration

`tbhbdev alpha agent` reads the optional `[agent]` table from the user config. Calling this command requires the table and every key shown below except `public_repos`, `max_run_minutes`, `docker`, `expires`, the host’s `tbhbdev` path, and the `[agent.codex]` table. Unreadable config fails with its path. Missing or invalid settings fail with the path, the full table name and the key, as `[agent.sandbox] audit_dir is required`. Unknown keys fail the same way at any level. `exec` validates a headless run before it contacts the host. It checks the remote version, then claims the name before it streams the inputs. A tmux session runs the host supervisor. Install that supervisor separately. `wait`, `status`, `kill`, `clean`, `keep`, `log` and `diff` use the recorded target. The parser allows and ignores `host_tag` in `[agent]` because another command reads it.

Use the hidden host command as `tbhbdev alpha agent run-host NAME [REQUEST] --host HOST [--prompt FILE] [--preflight]`. Supply a versioned JSON request through `REQUEST`, stdin with `REQUEST` set to `-`, or `<name>.request.json` by leaving out the path. A prompt path defaults to the run's recorded prompt. Config must list the host. During setup, run-host fetches a bare mirror over HTTPS under `$XDG_STATE_HOME/tbhbdev/agent-mirrors/github.com/<owner>/<repo>.git` and creates a separate clone at `<account clone>/.worktrees/agent-<name>`. It reads credentials through the secrets library. It then starts the harness with the policy's explicit environment and records its exit. Run files and the index live under `tbhbdev/agent-runs` in the state directory. SIGHUP, SIGTERM, and SIGINT stop the harness group and refresh Codex auth before run-host writes `<name>.rc`.

`public_repos` defaults to an empty list. List each public repository as `owner/name` or `github.com/owner/name`, using the same form as a request's `repo`. Run-host compares the parsed repository parts and refuses unlisted repositories before creating a mirror. Mirrors fetch from `https://github.com/<owner>/<name>.git` without credentials, prompts, SSH, or token fallback. Existing mirror configs switch to HTTPS on the next run. The local `--preflight` flag performs the same checks as the SSH verb.

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
public_repos = ["tbhb-dev/tools"]

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

Codex requires network proxy support (0.164.0 or later). Its proxy uses limited mode and denies Unix sockets. Only the exact hosts in `network_domains` have access, and the default list is empty. Claude builds use the same domain list and keep only the existing signing socket grant. Linux profiles resolve denied paths through symbolic links, keep existing paths, remove duplicates, and drop nested denies unless a read or write grant between the denies reopens the inner path. These paths include the Windows Subsystem for Linux, Docker, and user runtime sockets. A named `codex_network_proxy_opt_out` disables the proxy. Policy previews accept named opt-outs for agy or Codex full access. Run-host refuses agy. Authorized Codex full-access builds require the request, host, and config gates. Run-host announces full access with the authorization reference and appends a durable host taint marker before preparation. Tainted builds report `untrusted-host`. Run-host refuses tainted reviews. Reviews require reviews App references and refuse token request failures. A Codex network proxy opt-out leaves filesystem isolation in place. Run-host logs that exception on affected runs.

Run-host builds the harness command and adds the brief header. The driver sends the raw brief, an optional `<name>.schema.json`, and `--host` with a configured host name. Codex receives the schema path. Claude receives its contents. An unset, empty or relative `XDG_STATE_HOME` uses the home directory default. Finished runs keep their completion and log files when a rerun fails. Protected paths include shell startup files, Git and tmux config files, and socket parent directories below home.

For `[agent.sandbox]`, macOS hosts list `DARWIN_USER_TEMP_DIR` and `DARWIN_USER_CACHE_DIR` in `write_roots` as absolute paths. Linux hosts can list `/tmp`. The private `CODEX_HOME` stays outside these roots at `<state>/tbhbdev/agent-codex/<name>`, with mode 0700. Run-host removes it in `finish` after auth write-back. State defaults to `~/.local/state`.

Live socket probes include `denied-sockets-dir` under the configured socket directory and, on Linux, `denied-run-user` under `/run/user/<uid>`. Each label prints `refused`, `connected`, or `missing`. A connection, missing observation, or listener setup failure fails the probe. Temporary listener directories use mode 0700, and cleanup keeps existing parents.

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
- Every credential and profile `token` is a `vault/item/field` reference, and each part starts with a letter or digit, then continues with letters, digits, dots, `_` or `-`. `[agent.credentials.<account>]` takes one table for each account. `docker` and `gh` are optional. Every run sets `GH_CONFIG_DIR` to its private `TMPDIR/gh`, and build accounts never receive a GitHub token there, even with a `gh` reference. Publication uses a host-only credential pipe.
- Only `signing` also accepts a field label with internal spaces and `?ssh-format=openssh`, which converts the private key to OpenSSH format in memory.
- `signing` also accepts an absolute path to an OpenSSH private key file owned by the account with no group or world access, and the harness can't read that path.
- `default_profile` names one of the profiles, matching without regard to case, so `Primary` selects `primary`. Profile names that differ only in case fail validation. Display uses the name as written.
- `[[agent.host]]` lists at least one host. Its `name` is lower-case, dot-separated labels, `os` is `linux` or `macos`, `shell` is `bash` or `zsh`, `account` must be one of `accounts`, and host names are unique. Every name in `accounts` needs an `[agent.credentials.<name>]` table. `proven_harnesses` is a typed list of harness names. Add a harness only after a passing live run on that host. Remove it when upgrading that harness. Run-host refuses Claude while separate tool paths for Claude (H3) and the Claude credential decision remain open.

The run request is a versioned JSON object. Version 2 lists every run field, including the required boolean `tainted_host`, and no credential. Readers refuse version 1 and requests missing `tainted_host`. A reader refuses unknown versions, unknown or missing fields, and any `unsandboxed` request unless the harness is Codex, the profile is `build`, the host's `unsandboxed` is true and the authorization has the form `owner/repo#N` or `operator-chat-YYYY-MM-DD`. The repo argument takes the form `org/repo` or `<git host>/<org>/<repo>`, with `github.com` as the default git host, and resolves to `Code/<git host>/<org>/<repo>` under the home directory. A repo name can't be `.` or `..` or end in `.git`.

## Agent SSH interface

`tbhbdev alpha agent ssh-serve` is the forced command for the `agent` account. Add the controller's public key to that account's `authorized_keys` with this prefix, replacing `<agent home>` with its absolute home path:

```text
restrict,command="<agent home>/.local/bin/tbhbdev alpha agent ssh-serve" ssh-ed25519 PUBLIC_KEY
```

The controller selects its key and disables use of an identity agent:

```sh
ssh -i KEY -o IdentitiesOnly=yes -o IdentityAgent=none agent@HOST 'run-host NAME'
ssh -i KEY -o IdentitiesOnly=yes -o IdentityAgent=none agent@HOST 'preflight NAME'
ssh -i KEY -o IdentitiesOnly=yes -o IdentityAgent=none agent@HOST 'result NAME'
ssh -i KEY -o IdentitiesOnly=yes -o IdentityAgent=none agent@HOST 'kill NAME'
```

On macOS, the controller uses `/Library/Application Support/tbhbdev/agent-launch/id_ed25519` (`ssh_serve.MACOS_LAUNCH_KEY`). Linux uses `/etc/tbhbdev/agent-launch/id_ed25519` (`ssh_serve.LINUX_LAUNCH_KEY`). Quote the macOS path when passing it to `-i`.

`run-host NAME` and `preflight NAME` read one JSON object from stdin, at most 1 MiB:

```json
{"v": 1, "request": {"v": 2}, "push_token": null}
```

Replace `request` with the complete existing run-host request for a public GitHub repository. The request and envelope use separate versions. Prepare `NAME.prompt` in the existing run state directory before connecting. This interface uses run-host's current prompt and configuration files. It creates `NAME.request.json` with mode `0600` under `$XDG_STATE_HOME/tbhbdev/agent-runs`, which defaults to `$HOME/.local/state/tbhbdev/agent-runs`. Each entry claims `NAME.owner` with exclusive creation before launch. An existing owner or request refuses a second run of that name.

Supply a short-lived push token as `push_token`, or `null` to skip pushing. The SSH connection remains open until the run finishes. It shows run-host's normal output. After the harness exits with a new commit on `agent/NAME`, the host pushes that branch over GitHub HTTPS. A one-shot credential helper reads the token through an inherited pipe. The token stays out of arguments, environment variables, config files, and run files. Before it pushes, the host refuses a range with a merge commit or a head that isn't a fast-forward of the branch the run started on. It then scans the range for private names and runs gitleaks over it. The host also checks each commit: the run's signing key must have signed it, the agent must be its author and committer, and it must have no Signed-off-by or Co-authored-by trailer. A refusal ends the run with exit status 4 and a line in the run log. Push disables git hooks. A failed push records one error line and doesn't retry.

`preflight NAME` requires a null `push_token` and a fresh name, and reports every check with dependency skips and final counts after an uncounted Codex version line. Its workspace and credential checks include a sandbox `true` probe without launching an agent, with a nonzero exit for failures and informational `.bazel_env/bin` status.

`result NAME` prints one JSON object with `v`, `name`, `rc`, `state`, `report`, `log_tail`, `branch` and `head`. It reads the existing run files and limits the log to its last 200 lines. While a run continues, `rc` is `null`. Missing output fields use `null`. The `branch` field names only a branch the host pushed.

`kill NAME` stops the recorded process group after checking its identity. The supervisor records the run's completion. For a finished run it prints `already finished` and exits successfully.

| Exit code | Meaning |
| --- | --- |
| 0 | Successful command. |
| 2 | Refused command, account, envelope, or run data. |
| 3 | Unknown run name. |
| 4 | Branch push failed. |
| Other | The run-host exit status, including 1 for a refused run, 124 for a timeout, and 143 for an interrupted run. |

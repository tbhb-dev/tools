---
description: Agent settings, run requests, and host preparation.
generated:
  - role: generated
    harness: codex:0.171.0
    model: openai:gpt-6
    at: "2026-11-23T05:33:33Z"
status: stable
tags: [alpha]
title: Agent configuration
type: Reference
---

# Agent configuration

`tbhbdev alpha agent` reads the optional `[agent]` table from the user config. Calling this command requires the table and every key shown below except `public_repos`, `[agent.otel]`, `max_run_minutes`, `docker`, `expires`, `pull_requests_app_client_id`, `pull_requests_app_pem`, the host’s `unsandboxed` flag and `tbhbdev` path, and the `[agent.codex]` table. Unreadable config fails with its path. Missing or invalid settings fail with the path, the full table name and the key, as `[agent.sandbox] audit_dir is required`. Unknown keys fail the same way at any level. `exec` validates a headless run before it contacts the host. It checks the remote version, then claims the name before it streams the inputs. A tmux session runs the host supervisor. Install that supervisor separately. `wait`, `status`, `kill`, `clean`, `keep`, `log` and `diff` use the recorded target. The parser allows and ignores `host_tag` in `[agent]` because another command reads it.

`agent exec --fetch-ref REF` fetches a named ref during host preparation for either profile. Repeat the option up to eight times. Accepted refs are `refs/pull/<digits>/head` and `refs/heads/<branch>`. Branch names use only ASCII letters, digits, `.`, `_`, `/` and `-`; they cannot contain `..` or `//`, start or end with `/`, end with `.lock`, or start with `-`. The controller and host both validate these restrictions. For example, `--fetch-ref refs/pull/391/head --fetch-ref refs/heads/fix-392` makes `origin/fetched/pull/391/head` and `origin/fetched/heads/fix-392` available in the run clone. The host fetches these refs with the default branch in one Git fetch, using the existing authentication fallback. A missing requested ref fails preparation, and the error names the requested refs. The checkout stays on its usual build branch or review commit. No GitHub credential reaches the run through this option.

`agent exec` records the launching session's name and `CLAUDE_CODE_SESSION_ID` in the run record, and `agent status` prints `launcher=NAME`. The name is the tmux pane title of the launching process's pane, without its leading status glyph. A reviewer-role run, meaning `--role reviewer` or `--profile review`, starts only from a session on the review-launcher list, never from the session that owns the repository, and never when the name is missing. The built-in list names the `coord-review-N` sessions. `agent-review-launchers.json` in the state directory adds launchers and owners: `launchers` maps a session name to a list of `org/name` repositories, or to null for any repository, and `owners` maps a repository name to its owner session when that session isn't named for it. A malformed file refuses the launch. The list guards against honest mistakes by cooperating sessions; a session that forges its pane title isn't stopped.

`agent exec` reads the run host once before it transfers anything. It refuses the launch and prints the numbers when the 1-minute load reaches the host's load limit, when available memory falls under a share of total memory, or when used swap passes a share of total swap. Each `[[agent.host]]` table sets its own values. `load_limit` defaults to the host's logical CPU count, `min_memory_available_percent` defaults to 20 and `max_swap_used_percent` defaults to 80. Set a lower `load_limit` on a host that also runs services. `limits_warn_only = true` prints the same numbers as a warning and lets the launch continue. A failed or unreadable read refuses with `cannot read host limits`. The caller must pass `--ignore-limits` to launch anyway, or the host sets `limits_warn_only = true`, which turns the refusal into a warning. The run record keeps an `--ignore-limits` choice. Older run records without the field load as `false`.

`agent exec` exports one launch span named `tbhbdev alpha agent exec`. Its duration covers the launcher, and its launch attributes are `tbhb.agent.host`, `tbhb.agent.harness`, `tbhb.agent.model`, `tbhb.agent.profile`, `tbhb.agent.outcome` and `tbhb.run.id` (the run name). Outcome is `ok` for exit code zero, `refused` for a host-limit refusal and `failed` for another failure. The library accepts strings of at most 128 characters. These are the only launch attributes exported; the library also supplies command and service names, package version, exit code, error type and a drop counter. No prompt, prompt path, owner value, authorization, SSH output or other argument values are exported.

The package depends on `tbhb-telemetry` through a pinned GitHub release wheel URL. The lockfile records the wheel's SHA-256 digest.

`OTEL_EXPORTER_OTLP_ENDPOINT` selects the launch collector; its default is loopback port 4318. `TBHB_TELEMETRY_ALLOW_HOSTS` accepts a comma-separated list of exact collector host names to permit destinations beyond loopback. These environment variables are independent of `[agent.otel]`, which configures the harness. Export failure is silent, with one send attempt bounded to 250 milliseconds, and leaves the launch's output and exit status intact. Drop records live under `$XDG_STATE_HOME/tbhb/telemetry`, or `~/.local/state/tbhb/telemetry`. Set `OTEL_SDK_DISABLED=true` to disable launch telemetry. Other commands do not load the library.

Use the hidden host command as `tbhbdev alpha agent run-host NAME [REQUEST] --host HOST [--prompt FILE] [--preflight] [--owned]`. The driver's tmux start passes `--owned`. With the flag, run-host checks that `<name>.owner`, which the driver wrote, is a private regular file, and doesn't create it. The run ID itself never appears in a command line: the driver sends it as the first line of stdin to each remote script, which compares it with the file. Without the flag, run-host creates the owner file and refuses a name that already has one. Supply a versioned JSON request through `REQUEST`, stdin with `REQUEST` set to `-`, or `<name>.request.json` by leaving out the path. A prompt path defaults to the run's recorded prompt. Config must list the host. During setup, run-host fetches a bare mirror over HTTPS under `$XDG_STATE_HOME/tbhbdev/agent-mirrors/github.com/<owner>/<repo>.git` and creates a separate clone at `<account clone>/.worktrees/agent-<name>`. It reads credentials through the secrets library. It then starts the harness with the policy's explicit environment and records its exit. Run files and the index live under `tbhbdev/agent-runs` in the state directory. SIGHUP, SIGTERM, and SIGINT stop the harness group and refresh Codex auth before run-host writes `<name>.rc`. After the harness stops, whether the run succeeded or failed, run-host runs `bazelisk shutdown` (or `bazel shutdown`) in the run worktree so the run leaves no Bazel server behind. It waits up to 60 seconds and records `rc=N`, `timed out`, `failed to run` or `skipped` as `bazel_shutdown` in the completion index entry.

Every `tbhb-dev` repository on GitHub runs on any host with no list entry, and any other repository is refused before run-host creates a mirror. `public_repos` accepts only `tbhb-dev` entries (`owner/name` or `github.com/owner/name`) and the config refuses any other owner. Git never follows a redirect, so a repository that moved out of the organization fails the fetch. Mirrors fetch from `https://github.com/<owner>/<name>.git` without credentials, prompts, SSH, or token fallback. Existing mirror configs switch to HTTPS on the next run.

The ssh verb `preflight NAME` requires a null `push_token` and refuses a request with a token. The local `--preflight` flag ignores any `push_token`. It prepares the workspace and credentials and loads the signing agent. It logs whether the run worktree has `.bazel_env/bin` and warns that commit hooks fail when the directory is missing, without changing the exit status. After checking the policy and harness command, it writes `preflight ok: <executable path>` to the log. It stops the signing agent and records the exit status, phase, and index entry without launching the harness. Use a fresh name for the next run. Preparation errors use `run preparation failed at <step>: <cause>`. The cause keeps the validation message, the operating system error with only the filename's basename, or the first line of git's standard error. It removes control characters, redacts environment values, credentials and URL user information, and limits the cause to 200 characters. Other value errors and git failures without standard error use a generic cause.

A missing hook executable produces `run preparation failed at hook install: host executable prek was not found`. Install `prek` in a fixed directory on the host tools' search path, or add its directory to `host_path`.

When a new run worktree contains `mise.toml`, run-host calls `mise trust` with that file's path before credential delivery or harness launch. Concurrent setups take an exclusive `flock` on `mise-trust.lock` in the run host's state directory. The sandbox policy rejects writable roots that overlap this directory. If mise is missing, the lock fails, or the trust command fails, run-host records the reason in the run log and exits nonzero before starting the harness. A worktree without `mise.toml` proceeds directly to credential delivery.

The run clone has its own copy of the mirror's object store. The mirror never prunes objects. Run-host installs the identity and pre-commit hooks from its own templates without running code from the checkout. It finishes all git operations before starting the harness. Plain git in a run clone is unsafe for host processes.

Every `agent exec` run has the agent account’s full access. The account supplies the boundary for Codex, Claude and agy, for both build and review profiles. Runs skip sandbox grants, denied-path overlap checks, executable trust checks and write-root audits. The launcher offers no sandboxed mode.

Claude runs load user settings and the run’s generated settings. They ignore project settings and use only server configuration supplied for the run. The generated settings turn off hooks and the sandbox. Claude uses `bypassPermissions`. Codex marks each run clone as untrusted, which skips its project configuration.

Run-host records phase `unknown` when it can't prove the group stopped. It keeps the harness exit status and appends cleanup messages to the existing log.

```toml
[agent]
launch_wait_seconds = 120
agy_timeout = "900s"
kill_grace_seconds = 10
max_run_minutes = 120
public_repos = ["example-org/example-repo"]
sockets_dir = "/example/sockets"

[agent.sandbox]
write_roots = [".cache", ".local/share/mise", ".local/state/mise"]
network_domains = ["api.github.com", "otlp.example.net"]
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

[agent.otel]
endpoint = "https://otlp.example.net:4318"

[agent.credentials.agent]
docker = "example-vault/example-docker/config"
reviews_app_id = "example-vault/example-app/app_id"
reviews_app_client_id = "example-vault/example-app/client_id"
reviews_app_pem = "example-vault/example-app/private_key"
pull_requests_app_client_id = "example-vault/example-pr/client_id"
pull_requests_app_pem = "example-vault/example-pr/private_key"
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
# unsandboxed = true # Legacy flag; neither value gates a run.
accounts = ["agent"]
proven_harnesses = ["codex"]
```

Run-host finds harness executables through the account’s PATH, then `$HOME/.local/bin`, `$HOME/.local/gh-bin`, `$HOME/.bun/bin`, `$HOME/.npm-global/bin`, `/usr/local/bin`, `/opt/homebrew/bin` and the system directories. It uses the run account’s home for these paths. It ignores `sandbox.path` for executable lookup and grants no sandbox read or write paths.

Host tools use a fixed search path with optional absolute directories from `host_path`. Harness lookup keeps the `mise/shims` refusal: configure a fixed install directory on the account’s PATH. Remote launch and transfer scripts include the account’s `$HOME/.local/bin`, so they can find tmux there.

Codex uses the account’s existing executable. Runs skip standalone release validation and copying. A missing executable still fails with `host executable codex was not found`. Codex uses full access with its network proxy off. An agy review omits `--sandbox`.

Run-host announces full access and appends a durable host taint marker before preparation. Taint records supply audit information and never refuse a build or review because a host is unknown or tainted. Reviews still require reviews App references and refuse token request failures. A review token holds read permissions only (contents, pull requests, checks and statuses). Reviews App credentials can remain configured on any run host.

Run-host builds the harness command and adds the brief header. The driver sends the raw brief, an optional `<name>.schema.json`, and `--host` with a configured host name. Codex receives the schema path. Claude receives its contents. An unset, empty or relative `XDG_STATE_HOME` uses the home directory default. Finished runs keep their completion and log files when a rerun fails.

The parser still accepts the legacy `[agent.sandbox]` settings for compatibility and policy probes. `agent exec` skips their enforcement. The private `CODEX_HOME` lives at `<state>/tbhbdev/agent-codex/<name>`, with mode 0700. Run-host removes it in `finish` after auth write-back. State defaults to `~/.local/state`.

Live Codex probes copy the account's auth through `credentials.auth_copy`, then use the production refresh write-back and removal path. Each probe asserts that its test directory contains no `auth.json` when it finishes. Each probe plants hooks only under the scratch run's home.

The opt-in sandbox probes test the legacy policy builders separately from the default launch behavior. Run them from a clean checkout with this command, outside an agent sandbox. The state directory keeps the test root outside the system temp directories that the probes grant for writing.

```sh
TBHBDEV_SANDBOX_LIVE=1 uv run pytest --basetemp "$HOME/.local/state/tbhbdev/sandbox-live" tests/test_alpha_agent_sandbox_live.py tests/test_alpha_agent_sandbox_live_host.py tests/test_alpha_agent_git_parent.py tests/test_alpha_agent_git_commit_live.py
```

During the build probe, the installed identity hook stamps a commit with a `Generated-by` trailer. Both profiles attempt writes to six protected Git children, then compare each path with its host snapshot.

The settings have these constraints:

- The connection settings default to `ssh_connect_timeout = 15`, `ssh_keepalive_seconds = 30` and `ssh_keepalive_count_max = 3`. Each is a positive integer. Codex's `web_search` also has a default. The `[agent.codex]` table and each of its keys are optional, and each profile, `build` and `review`, defaults to `live`. A key can narrow it to `cached` or `disabled`.
- `[agent.otel]` is optional and takes one key, `endpoint`, a base `https` URL with no credentials, path, query or fragment, such as `https://otlp.example.net:4318`. Its host must also appear in `[agent.sandbox] network_domains`. That list matches hosts only, so it limits no port. The parser refuses control characters, a non-numeric, zero, zero-padded or out-of-range port and a malformed authority and quotes nothing from the value in its error. With it set, each Codex run's private `CODEX_HOME` gets a `config.toml` with an `[otel]` table that exports traces to `<endpoint>/v1/traces` and metrics to `<endpoint>/v1/metrics` over `otlp-http`. The table has no headers and sets `log_user_prompt = false`. It leaves the log exporter off, because Codex logs tool arguments and an output preview, which can hold a credential or a file's contents, and `log_user_prompt` guards only the prompt text. Whether to export logs is a later decision. Every run with an endpoint also gets `OTEL_RESOURCE_ATTRIBUTES` with `host.name` and `tbhb.account`, and a run without one gets neither the file nor the variable. Tests check the generated file and the variable. A live check against codex-cli 0.167.2 loaded the generated file from a scratch `CODEX_HOME` and accepted it, and the same file with `protocol` set to an invalid value failed with `unknown variant` at `otel.trace_exporter.protocol`, so Codex reads the exporter tables as written. The export itself is unverified end to end, as are two claims about Codex: that it honors the endpoint, and that its traces leave out tool arguments. The private `CODEX_HOME` has mode `0700`. The table contains only the endpoint URL and the prompt flag, so the endpoint's reachability is the boundary.
- `launch_wait_seconds`, `kill_grace_seconds` and `max_run_minutes` are positive integers. `agy_timeout` and the optional `agy_build_timeout` are bare seconds or a duration ending in `s`, `m` or `h`, up to 7 days. An agy review run uses `agy_timeout`, and an agy build run uses `agy_build_timeout`, which defaults to `3600s`. `--timeout` overrides both.
- Path settings are non-blank strings without a null byte or a `..` part. The sandbox lists must be non-empty.
- Every credential and profile `token` is a `vault/item/field` reference, and each part starts with a letter or digit, then continues with letters, digits, dots, `_` or `-`. `[agent.credentials.<account>]` takes one table for each account, and `docker` and `gh` are optional. `pull_requests_app_client_id` and `pull_requests_app_pem` are optional too, and a table sets both or neither. A table that sets them can't also set `gh`. A build run whose account has no `gh` requests a GitHub token from that App, limited to the run's repository and to the `contents: read` permission, and writes it to the run's `gh/hosts.yml`. A build run whose account has neither doesn't get a GitHub credential. If the request fails, the launcher refuses the run with `pull-requests App token unavailable; build refused`. The token expires after an hour, and the launcher neither refreshes nor revokes it.
- Only `signing` also accepts a field label with internal spaces and `?ssh-format=openssh`, which converts the private key to OpenSSH format in memory.
- `default_profile` names one of the profiles, matching without regard to case, so `Primary` selects `primary`. Profile names that differ only in case fail validation. Display uses the name as written.
- `[[agent.host]]` lists at least one host. Its `name` is lower-case, dot-separated labels, `os` is `linux` or `macos`, `shell` is `bash` or `zsh`, `account` must be one of `accounts`, and host names are unique. Every name in `accounts` needs an `[agent.credentials.<name>]` table. `proven_harnesses` records `codex`, `claude` and `agy` names. Add a harness after one successful, real `agent exec` run on that host. An empty list never refuses a run. Remove the recorded fact when upgrading that harness.

The run request is a versioned JSON object. Version 2 lists every run field, including the required boolean `tainted_host`, and no credential. Readers refuse version 1 and requests missing `tainted_host`. A reader refuses unknown versions, unknown or missing fields. An optional authorization must have the form `owner/repo#N` or `operator-chat-YYYY-MM-DD`. The repo argument takes the form `org/repo` or `<git host>/<org>/<repo>`, with `github.com` as the default git host, and resolves to `Code/<git host>/<org>/<repo>` under the home directory. A repo name can't be `.` or `..` or end in `.git`.

Every `agent exec` request sets `unsandboxed` and `tainted_host` to true. Omit `--unsandboxed` for the default behavior. Existing callers may still pass `agent exec --unsandboxed operator-chat-YYYY-MM-DD` or `--unsandboxed owner/repo#N` to record `authorized_by`. The host’s legacy `unsandboxed` key is optional, and neither value refuses a run. Run-host reads the account’s config before parsing the request.

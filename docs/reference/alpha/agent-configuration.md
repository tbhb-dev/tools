---
description: Agent settings, run requests, and host preparation.
generated:
  - role: generated
    harness: codex:0.171.0
    model: openai:gpt-6.2-sol
    surface: exec
    session: sha-256-64:1a6d4168da557dcf
    at: "2026-11-26T01:58:42Z"
status: stable
tags: [alpha]
title: Agent configuration
type: Reference
---

# Agent configuration

`tbhbdev alpha agent` reads the optional `[agent]` table from the user config. Calling this command requires the table and every key shown below except `public_repos`, `[agent.otel]`, `max_run_minutes`, `docker`, `expires`, `pull_requests_app_client_id`, `pull_requests_app_pem`, the host’s `unsandboxed` flag and `tbhbdev` path, the `[agent.codex]` table and the `[agent.worker]` table. Unreadable config fails with its path. Missing or invalid settings fail with the path, the full table name and the key, as `[agent.sandbox] audit_dir is required`. Unknown keys fail the same way at any level. `exec` validates a headless run before it contacts the host. It checks the remote version, then claims the name before it streams the inputs. A tmux session runs the host supervisor. Install that supervisor separately. `wait`, `status`, `kill`, `clean`, `keep`, `log` and `diff` use the recorded target. The parser allows and ignores `host_tag` in `[agent]` because another command reads it.

`agent exec --fetch-ref REF` fetches a named ref during host preparation for either profile. Repeat the option up to eight times. Accepted refs are `refs/pull/<digits>/head` and `refs/heads/<branch>`. Branch names use only ASCII letters, digits, `.`, `_`, `/` and `-`; they cannot contain `..` or `//`, start or end with `/`, end with `.lock`, or start with `-`. The controller and host both validate these restrictions. For example, `--fetch-ref refs/pull/391/head --fetch-ref refs/heads/fix-392` makes `origin/fetched/pull/391/head` and `origin/fetched/heads/fix-392` available in the run clone. The host fetches these refs with the default branch in one Git fetch, using the existing authentication fallback. A missing requested ref fails preparation, and the error names the requested refs. The checkout stays on its usual build branch or review commit. No GitHub credential reaches the run through this option.

The launcher warns when the prompt mentions a ref that no `--fetch-ref` option covers.

`agent exec --expect-commit` requires a new commit from a build run. If the run would otherwise succeed and the host records no new commit, it returns exit status 6 and logs `no new commit: the brief expected one (--expect-commit)`. The launcher refuses this flag for the review profile.

The Stop hook checks the run worktree for uncommitted tracked changes and the run branch for a commit past its starting point. Use `agent exec --no-expect-commit` for a build that can finish without a new commit. The worktree still has to be clean of tracked changes. For reviews, the hook requires a standalone `VERDICT: GO` or `VERDICT: BLOCK` line in the final reply, with an optional full commit SHA. The hook asks the model to continue up to three times per run, then allows the stop and logs the reason.

Pass `--ref <full sha>` to launch a review and include `--reason` when you use Claude.

After workspace preparation, a build run reads the repository’s hook configs for references to Bazel shim directories and executables. If a referenced directory is missing, the host reports phase `shims` and uses `bazelisk` or `bazel` to run the `bazel_env` target for each referenced directory’s package, with a 10-minute timeout per target. The harness can start once the referenced executables exist, or once the directory exists if the hooks reference only the directory, even if Bazel returns a nonzero status. Review runs and preflight checks skip this step.

`agent exec` records the launching session's name and `CLAUDE_CODE_SESSION_ID` in the run record, and `agent status` prints `launcher=NAME`. The name is the tmux pane title of the launching process's pane, without its leading status glyph. Any named session may launch a reviewer-role run, meaning `--role reviewer` or `--profile review`, including for its own repository. A missing `TMUX_PANE` or pane title refuses the launch; set the launching session's tmux pane title to its name. Review tier rules are enforced by briefs.

A caller with no tmux pane, such as the build-loop worker, can launch a review as a registered worker. Register the worker's executable in the optional `[agent.worker]` table with `path`, an absolute path that has no default; a relative path fails with the config path, and with no table no worker is registered. Then pass `--worker-workflow ID`, where `ID` is 1 to 128 of `A-Za-z0-9._:-`. The launch counts as the worker only when an ancestor process of `agent exec`, found by walking parent pids up to pid 1, runs an executable whose real path equals the real path of `path`. The walk reads `/proc/PID/stat` and `/proc/PID/exe` on Linux, and `ps` on macOS. A failed lookup or a loop in the parents means no match. The run record then names the launcher `worker/ID`, and `agent status` prints `launcher=worker/ID`. The flag applies to reviewer-role runs only, and a tmux pane keeps priority over it. Without a match, the flag fails with the reason. Any other caller with no pane fails as before. The match checks the executable path alone, so any process of the same user that runs that executable passes.

`agent exec` reads the run host once before it transfers anything. It refuses the launch and prints the numbers when the 1-minute load reaches the host's load limit, when available memory falls under a share of total memory, or when used swap passes a share of total swap. Each `[[agent.host]]` table sets its own values. `load_limit` defaults to the host's logical CPU count, `min_memory_available_percent` defaults to 20 and `max_swap_used_percent` defaults to 80. Set a lower `load_limit` on a host that also runs services. `limits_warn_only = true` prints the same numbers as a warning and lets the launch continue. A failed or unreadable read refuses with `cannot read host limits`. The caller must pass `--ignore-limits` to launch anyway, or the host sets `limits_warn_only = true`, which turns the refusal into a warning. The run record keeps an `--ignore-limits` choice. Older run records without the field load as `false`.

Each `[[agent.host]]` can set `build_shims` to a Boolean. It defaults to `true`. Set it to `false` on hosts that cannot run Bazel. Those hosts refuse builds that need hook shims.

`[agent]` sets the optional memory reservations as positive integers in MiB. `run_memory_mib` sets the build reservation and defaults to 6144 MiB (6 GiB). `review_memory_mib` defaults to the configured build reservation. The launch check also requires available memory to meet the reservation for the requested profile. Linux reads `MemAvailable`. On macOS, the check sums four counts from `vm_stat`, `Pages free`, `Pages inactive`, `Pages speculative`, and `Pages purgeable`, and multiplies the sum by the page size.

Each `[[agent.host]]` can set `max_concurrent_runs` to a non-negative integer. It defaults to 3. Set it to 0 for a host that only runs CI, or to 1 for a host that only runs reviews. The host supervisor reserves a slot from preparation through cleanup. A limit of 3 allows up to 2 builds and leaves a slot for the `review` profile. Reviews can also use any available slots that builds can use. A launch with no eligible slot fails immediately. `--ignore-limits` and `limits_warn_only` apply to the load, memory, and swap checks. Use the same slot limit in every account's configuration on the host.

`agent exec` exports one launch span named `tbhbdev alpha agent exec`. Its duration covers the launcher, and its launch attributes are `tbhb.agent.host`, `tbhb.agent.harness`, `tbhb.agent.model`, `tbhb.agent.profile`, `tbhb.agent.outcome` and `tbhb.run.id` (the run name). Outcome is `ok` for exit code zero, `refused` for a host-limit refusal and `failed` for another failure. The library accepts strings of at most 128 characters. These are the only launch attributes exported; the library also supplies command and service names, package version, exit code, error type and a drop counter. No prompt, prompt path, owner value, authorization, SSH output or other argument values are exported.

The package depends on `tbhb-telemetry` through a pinned GitHub release wheel URL. The lockfile records the wheel's SHA-256 digest.

`OTEL_EXPORTER_OTLP_ENDPOINT` selects the launch collector; its default is loopback port 4318. `TBHB_TELEMETRY_ENDPOINT` supplies the endpoint when `OTEL_EXPORTER_OTLP_ENDPOINT` is unset or empty, for launchers that can't pass `OTEL_*` variables. `TBHB_TELEMETRY_ALLOW_HOSTS` accepts a comma-separated list of exact collector host names to permit destinations beyond loopback. These environment variables are independent of `[agent.otel]`, which configures the harness. Export failure is silent, with one send attempt bounded to 250 milliseconds, and leaves the launch's output and exit status intact. Drop records live under `$XDG_STATE_HOME/tbhb/telemetry`, or `~/.local/state/tbhb/telemetry`. Set `OTEL_SDK_DISABLED=true` to disable launch telemetry. Other commands do not load the library.

After applying the endpoint fallback, `agent exec` looks for non-empty process environment values for `OTEL_EXPORTER_OTLP_ENDPOINT` and `TBHB_TELEMETRY_ALLOW_HOSTS`. For each key still missing, it checks the `env` object in Claude Code's managed settings file (`/Library/Application Support/ClaudeCode/managed-settings.json` on macOS or `/etc/claude-code/managed-settings.json` on Linux). It then checks the `env` object in the user's `settings.json` under `$CLAUDE_CONFIG_DIR` or the home directory's `.claude` folder. It looks up only these two keys in the settings files. Once per process, it prints each key's source to stderr and warns that the exporter may drop the launch trace if either key has no source. The line contains no values. `OTEL_SDK_DISABLED=true` skips the lookup and the line.

Use the hidden host command as `tbhbdev alpha agent run-host NAME [REQUEST] --host HOST [--prompt FILE] [--preflight] [--owned]`. The driver's tmux start passes `--owned`. With the flag, run-host checks that `<name>.owner`, which the driver wrote, is a private regular file, and doesn't create it. The run ID itself never appears in a command line: the driver sends it as the first line of stdin to each remote script, which compares it with the file. Without the flag, run-host creates the owner file and refuses a name that already has one. Supply a versioned JSON request through `REQUEST`, stdin with `REQUEST` set to `-`, or `<name>.request.json` by leaving out the path. A prompt path defaults to the run's recorded prompt. Config must list the host. During setup, run-host fetches a bare mirror over HTTPS under `$XDG_STATE_HOME/tbhbdev/agent-mirrors/github.com/<owner>/<repo>.git` and creates a separate clone at `<account clone>/.worktrees/agent-<name>`. It reads credentials through the secrets library. It then starts the harness with the policy's explicit environment and records its exit. Run files and the index live under `tbhbdev/agent-runs` in the state directory. SIGHUP, SIGTERM, and SIGINT stop the harness group and refresh Codex auth before run-host writes `<name>.rc`.

At the end of a run, the supervisor lists the agent account's processes before stopping the harness group, then shuts the Bazel servers down after the group has stopped. A server that is still alive after a shutdown that returned 0, which happens while the server starts, gets up to three more attempts two seconds apart. On macOS and Linux, it uses process ancestry, harness group membership, or the Bazel workspace path to identify the run's servers. The supervisor accepts workspace paths at or below the run clone root, including checkouts in `.worktrees`. It checks Bazel's process ID file against the process listing before it attempts shutdown. If the process listing fails, it uses the run's recorded output bases.

The supervisor calls `bazelisk shutdown` or `bazel shutdown` with each output base and allows up to 60 seconds per command. It logs the base path and result for each attempt, then lists the distinct results in the `bazel_shutdown` field of the completion index entry. A failed shutdown leaves the run's exit status intact and the supervisor continues with the remaining bases. Only the recorded bases qualify for deletion after shutdown.

Every `tbhb-dev` repository on GitHub runs on any host with no list entry, and any other repository is refused before run-host creates a mirror. `public_repos` accepts only `tbhb-dev` entries (`owner/name` or `github.com/owner/name`) and the config refuses any other owner. Git never follows a redirect, so a repository that moved out of the organization fails the fetch. Mirrors fetch from `https://github.com/<owner>/<name>.git` without credentials, prompts, SSH, or token fallback. Existing mirror configs switch to HTTPS on the next run.

The ssh verb `preflight NAME` requires a null `push_token` and refuses a request with a token. The local `--preflight` flag ignores any `push_token`. It prepares the workspace and credentials and loads the signing agent. It logs whether the run worktree has `.bazel_env/bin` and warns that commit hooks fail when the directory is missing, without changing the exit status. After checking the policy and harness command, it writes `preflight ok: <executable path>` to the log. It stops the signing agent and records the exit status, phase, and index entry without launching the harness. Use a fresh name for the next run. Preparation errors use `run preparation failed at <step>: <cause>`. The cause keeps the validation message, the operating system error with only the filename's basename, or the first line of git's standard error. It removes control characters, redacts environment values, credentials and URL user information, and limits the cause to 200 characters. Other value errors and git failures without standard error use a generic cause.

Preparation redacts the search path it reports when a hook executable is missing. Check the run log for the failing step and `host_tools.search_path` for the search directories. Install `prek` in a fixed directory on the host tools' search path, or add its directory to `host_path`.

The run clone has its own copy of the mirror's object store. The mirror never prunes objects. Run-host installs the identity and pre-commit hooks from its own templates without running code from the checkout. It finishes all git operations before starting the harness. Plain git in a run clone is unsafe for host processes.

Every `agent exec` run has the agent account’s full access. The account supplies the boundary for Codex, Claude and agy, for both build and review profiles. Runs skip sandbox grants, denied-path overlap checks, executable trust checks and write-root audits. The launcher offers no sandboxed mode.

`tbhbdev alpha install` registers Stop alongside the other user-level hooks for Claude, Codex, and Antigravity. Headless Claude runs use their generated settings with the Stop hook enabled and skip user and project settings. Headless Codex runs install and trust the Stop hook in their private configuration. Codex marks each run clone as untrusted, which skips its project configuration. Antigravity runs use the installed user-level Stop hook.

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
endpoint = "http://otlp.example.net:4318"

[agent.credentials.agent]
docker = "example-vault/example-docker/config"
reviews_app_id = "example-vault/example-app/app_id"
reviews_app_client_id = "example-vault/example-app/client_id"
reviews_app_pem = "example-vault/example-app/private_key"
pull_requests_app_client_id = "example-vault/example-pr/client_id"
pull_requests_app_pem = "example-vault/example-pr/private_key"
signing = "example-vault/example-signing/private_key"

# [agent.worker]
# path = "/opt/example-worker/bin/worker"

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

Codex uses the account’s existing executable. Runs skip standalone release validation and copying. A missing executable still fails with `host executable codex was not found on PATH <directories>`. Codex uses full access with its network proxy off. An agy review omits `--sandbox`.

Run-host announces full access before preparation. Reviews still require reviews App references and refuse token request failures. A review token holds read permissions only (contents, pull requests, checks and statuses). Reviews App credentials can remain configured on any run host.

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
- `[agent.otel]` is optional and takes one key, `endpoint`, a base URL with no credentials, path, query or fragment. Use `http` for a private-network collector such as `http://otlp.example.net:4318`, and use `https` elsewhere. For either scheme, the host must appear in `[agent.sandbox] network_domains`. That list matches hosts only, so it limits no port. The parser refuses control characters, a non-numeric, zero, zero-padded or out-of-range port and a malformed authority and quotes nothing from the value in its error. With it set, each Codex run's private `CODEX_HOME` gets a `config.toml` with an `[otel]` table that exports traces to `<endpoint>/v1/traces` and metrics to `<endpoint>/v1/metrics` over `otlp-http`. The table has no headers and sets `log_user_prompt = false`. It leaves the log exporter off, because Codex logs tool arguments and an output preview, which can hold a credential or a file's contents, and `log_user_prompt` guards only the prompt text. Whether to export logs is a later decision. Every run with an endpoint also gets `OTEL_RESOURCE_ATTRIBUTES` with `host.name`, `tbhb.account` and `tbhb.run.id`, and a run without one gets neither the file nor the variable. Tests check the generated file and the variable. A live check against codex-cli 0.167.2 loaded the generated file from a scratch `CODEX_HOME` and accepted it, and the same file with `protocol` set to an invalid value failed with `unknown variant` at `otel.trace_exporter.protocol`, so Codex reads the exporter tables as written. The export itself is unverified end to end, as are two claims about Codex: that it honors the endpoint, and that its traces leave out tool arguments. The private `CODEX_HOME` has mode `0700`. The table contains only the endpoint URL and the prompt flag, so the endpoint's reachability is the boundary.
- `launch_wait_seconds`, `kill_grace_seconds` and `max_run_minutes` are positive integers. `agy_timeout` and the optional `agy_build_timeout` are bare seconds or a duration ending in `s`, `m` or `h`, up to 7 days. An agy review run uses `agy_timeout`, and an agy build run uses `agy_build_timeout`, which defaults to `3600s`. `--timeout` overrides both.
- Path settings are non-blank strings without a null byte or a `..` part. The sandbox lists must be non-empty.
- Every credential and profile `token` is a `vault/item/field` reference, and each part starts with a letter or digit, then continues with letters, digits, dots, `_` or `-`. `[agent.credentials.<account>]` takes one table for each account, and `docker` and `gh` are optional. `pull_requests_app_client_id` and `pull_requests_app_pem` are optional too, and a table sets both or neither. A table that sets them can't also set `gh`. A build run whose account has no `gh` requests a GitHub token from that App, limited to the run's repository and to `contents: write`, `pull_requests: write`, and `workflows: write`, and writes it to the run's `gh/hosts.yml`. A build run whose account has neither doesn't get a GitHub credential. If the request fails, the launcher refuses the run with `pull-requests App token unavailable; build refused`. The token expires after an hour, and the launcher neither refreshes nor revokes it.
- Only `signing` also accepts a field label with internal spaces and `?ssh-format=openssh`, which converts the private key to OpenSSH format in memory.
- `default_profile` names one of the profiles, matching without regard to case, so `Primary` selects `primary`. Profile names that differ only in case fail validation. Display uses the name as written.
- `[[agent.host]]` lists at least one host. Its `name` is lower-case, dot-separated labels, `os` is `linux` or `macos`, `shell` is `bash` or `zsh`, `account` must be one of `accounts`, and host names are unique. Every name in `accounts` needs an `[agent.credentials.<name>]` table. `proven_harnesses` records `codex`, `claude` and `agy` names. Add a harness after one successful, real `agent exec` run on that host. An empty list never refuses a run. Remove the recorded fact when upgrading that harness.

The run request is a versioned JSON object. Version 2 lists every run field and no credential. Readers refuse version 1. A reader refuses unknown versions, unknown or missing fields. An optional authorization must have the form `owner/repo#N` or `operator-chat-YYYY-MM-DD`. The repo argument takes the form `org/repo` or `<git host>/<org>/<repo>`, with `github.com` as the default git host, and resolves to `Code/<git host>/<org>/<repo>` under the home directory. A repo name can't be `.` or `..` or end in `.git`.

Every `agent exec` request sets `unsandboxed` to true. Omit `--unsandboxed` for the default behavior. Existing callers may still pass `agent exec --unsandboxed operator-chat-YYYY-MM-DD` or `--unsandboxed owner/repo#N` to record `authorized_by`. The host’s legacy `unsandboxed` key is optional, and neither value refuses a run. Run-host reads the account’s config before parsing the request.

---
description: Agent isolation constraints, coordination, and cleanup.
generated:
  - role: generated
    harness: codex:0.171.0
    model: openai:gpt-6.2-sol
    surface: exec
    session: sha-256-64:6e402d15023f0de1
    at: "2026-11-26T01:38:57Z"
status: stable
tags: [alpha]
title: Agent isolation limits
type: Reference
---

# Agent isolation limits

Run-host runs git in a run clone only during `clone_run` and `create`, before launch. After the harness starts, run-host never runs git in that clone. The controller's `diff` command can read committed work there through the run account's SSH connection. An operator who runs `git add -A` or `git stash` in a run clone can run a nested repository's clean filter. The port never runs those commands in a run clone.

Run-host checks out a new build branch from a commit and leaves upstream tracking unset before launch. The legacy sandbox probes keep `.git/config` read-only. In those probes, use `git switch --no-track -c x origin/main` and push with an explicit destination branch, such as `git push origin HEAD:refs/heads/x`.

Every `agent exec` run has the account’s full access. It can read the account’s 1Password Connect token and every secret the account can read, and write anything the account can write. Codex, Claude and agy use this behavior for both build and review profiles. The agent account supplies the boundary. A child can leave the process group and keep running after the run ends.
Runs skip sandbox policy checks, executable trust checks and write-root audits. They still refuse mise shims.

- A run can read other runs' temp directories.
- Build runs have access to the account's `gh` token and Docker config.
- A run's tools can read its private Codex auth file.
- The agent can write the final answer file.
- Run-host sets Codex's `web_search` mode and removes the private `CODEX_HOME` at finish. It has no separate web search store or cleanup for any state that Codex or its provider keeps outside that home.

The coordinator traced every git process that the Codex parent starts, with every Git plant from the regression tests present. With codex-cli 0.166.0 on macOS and 0.164.0 on Linux, each session started 12 git processes before any tool call: `rev-parse HEAD` (3), `rev-parse --git-dir` (2), `rev-parse --abbrev-ref HEAD` (2), `remote get-url origin` (2), `remote -v` (1), `config --null --get core.fsmonitor` (1) and `status --porcelain` (1). Each one used Codex's own `-c safe.bareRepository=explicit -c core.hooksPath=/dev/null -c core.fsmonitor=false`. None started after a tool call. Version 0.164.0 also cloned its plugin repository into `CODEX_HOME`'s temp directory and ran `git maintenance run --auto` there. Re-check these observations on every Codex upgrade.

The legacy sandbox probes also exercise Git pointer files and terminal isolation. They test the policy builders separately from `agent exec`, which launches every harness without a sandbox.

Launch a run with a local prompt file:

```sh
tbhbdev alpha agent exec --host example-linux --repo example-org/example-repo --model example-model --effort high --profile build example brief.md
tbhbdev alpha agent status example
tbhbdev alpha agent wait example
```

Reviews accept `--ref` and resolve it before launch. `--schema` streams a JSON schema beside the prompt. Claude requires `--reason` and accepts `--claude-profile`. Use a `gemini-*` model for agy, or pass `--allow-non-gemini`. `--ops` selects the host's configured `ops` account.

Each run gets a private `TMPDIR`. Run-host sets `PYTEST_DEBUG_TEMPROOT` to the same directory for pytest's default base directory. At exit, run-host removes the run's temporary tree without following symlinks. If removal fails, the run keeps its exit status.

The optional `[[agent.host]] tbhbdev` key names an absolute executable path on that host. Its default is `$HOME/.local/bin/tbhbdev`, using the remote account’s home. Local `ssh` and `tmux` resolve against a fixed system PATH. Remote launch and transfer scripts add the account’s `$HOME/.local/bin` and the standard package directories on macOS, and skip login shell startup.

The coordinator stores metadata under `$XDG_STATE_HOME/tbhbdev/agent-runs/` on the coordinator. The remote directory uses the same location under the run account, with `.local/state` under home as the default state directory. The transfer claims `<name>.owner` with `set -C` before extracting inputs. It refuses an existing session or any `<name>.*` file with exit status 73. A collision never kills the existing session.

The tmux socket is `-S <sockets_dir>/agents`. The driver creates the configured directory with mode 0700 and refuses a symlink, another owner, or any other mode. Sandbox policies deny reads there. Whether each sandbox blocks a Unix socket connection needs the shadow test.

`wait` prefers the final answer and returns the run's exit status. Its poll interval defaults to 30 seconds (`TBHB_AGENT_WAIT_POLL`). Consecutive connection failures stop after 240 seconds (`TBHB_AGENT_WAIT_UNREACHABLE`) with exit status 75. Exit codes 1 and 75 overlap with harness exit codes. Read the first result line to distinguish an unknown or unreachable run from a harness result. Agent text follows that line.

The build harness pushes its branch and opens any new pull request. With `--start-ref`, run-host fast-forwards the selected remote branch after the harness succeeds.

For a build using its own branch, run-host checks for new commits after the harness succeeds. It compares local branch tips and detached `HEAD` with the history recorded before launch, including refs fetched during preparation. Remote-tracking refs added or changed during the harness run do not affect that history. Run-host records the new tip for the checked-out run branch when available. When the run branch is not checked out, it can record a new tip from another local branch or detached `HEAD`.

| Exit status | Result |
| --- | --- |
| 0 | The build meets its commit and push requirements, or the host posts the review. |
| 4 | The commit check or the `--start-ref` push fails. The log starts the reason line with `commit check failed:`, `push skipped:`, or `push failed:`. |
| 6 | The build produced no new commit with `--expect-commit` enabled by default. The log starts the reason line with `no new commit:`. |
| 9 | The build's own branch did not get a new tip on origin or the host could not verify it. The log starts the reason line with `push failed:`. |
| 10 | The harness succeeded but the host did not post a review. The log starts the reason line with `review not posted:`. |

The commit check refuses new tips outside the starting commit's ancestry and lists their count and short commit IDs in the log. It also refuses a checked-out run branch when new commits exist only on other branches.

If the harness fails, run-host keeps its exit status. `tbhbdev alpha agent status NAME` shows the last publication reason next to the return code when the run ends with a nonzero status and the log contains a reason.

Each claim writes a random run ID to the local metadata and the remote `<name>.owner` file before extracting inputs. Every remote command reads the ID from the first line of its stdin and compares it with `<name>.owner`, so the ID never appears in an ssh argument list or a host audit log. The tmux start passes run-host `--owned`, and run-host checks `<name>.owner` first. The file must be a regular file, never a symlink. Its mode must be 0600 and its owner the running user. Its content must be the ID followed by a newline. An empty ID, a missing file, a directory, a named pipe or any other content refuses the start with exit status 1 and leaves the earlier run's files untouched. Run-host writes its log and return code for a failed start only while it holds `<name>.run.lock`. A refusal before that point leaves every record unchanged. Later commands check that ID before they act. A mismatch or missing owner archives local metadata and reports `not owner`. After a transfer error, the driver retains metadata only when a remote owner check succeeds. The driver sends the raw brief and starts run-host with the configured `--host`. Every tmux call uses `-f /dev/null`, and session creation passes an absolute executable with separate arguments.

`kill` closes the tmux session to notify the supervisor with SIGHUP. The supervisor stops its children before it records the return code. The coordinator writes 137 only after the session closes and the grace period elapses without a return code. It also writes `<name>.rc.fallback`, which makes `clean` require `--force` because processes might still run.

The host keeps a bare mirror at `$XDG_STATE_HOME/tbhbdev/agent-mirrors/<git host>/<owner>/<repo>.git`. An empty or unset `XDG_STATE_HOME` uses `~/.local/state`. Only host git touches the mirror, under its `agent-mirror.lock`. Before launch, the supervisor makes a separate run clone at `<account clone>/.worktrees/agent-<name>`. That clone has its own `.git` directory. The legacy sandbox denies writes to `.git/config`, `.git/hooks` and `.git/info`. Inspection output from both repositories is untrusted.

```text
Don't run git inside a run clone: it is agent-written. Use `tbhbdev alpha agent log|diff NAME`.
```

`log NAME` shows up to ten pushed commits by default. `log -n 5 NAME` changes that count. When `agent/<name>` exists on origin, `diff NAME` shows changes from the default branch's merge base to that pushed branch. These reads fetch the origin's default branch and `agent/<name>` into the mirror, then inspect those refs. If the branch is absent on origin, `diff NAME` reads the run's own clone through the recorded run account's SSH connection and prints `git format-patch --stdout refs/remotes/origin/<default>..HEAD`. It resolves the default branch name on origin and uses that branch’s ref saved in the run clone at startup. This includes local commits and omits uncommitted changes; no commits produces empty output. The fallback never lets Git read the run's own configuration. It reads `HEAD`, the loose ref files, and `packed-refs` as plain text and checks each value is a 40-digit hash. It then runs `format-patch` in a new empty bare repository in a temporary directory that borrows only the run's object directory, so settings such as `include.path`, `format.from`, `format.signatureFile`, and `commondir` in the run clone have no effect. The fallback refuses a symlink in the clone path, in the git files it reads, and anywhere under `objects` and `refs`, and it refuses an unreadable `objects` or `refs` directory, an `objects/info/alternates` file, and a multi-pack index. The new repository also skips the user's global Git configuration. It passes `--no-signature`, `--no-attach`, `--no-add-header`, and `--no-notes`. The symlink scan and the read aren't atomic, so a run that's still writing can swap a path between them. Both paths disable external diff, text conversion, and color, cap output at 65536 bytes, and filter terminal controls. Neither `wait` nor `status` needs the mirror. The mirror never prunes, so disk use grows with history until a pruning follow-up.

Cleanup refuses an open session. Every new run requires `clean --force`, which moves the run clone and per-run files into dated trash without reading agent-written git state. Cleanup refuses a symlink in place of the run clone. Retries skip completed moves, and removing all files for the run permits name reuse. `keep` succeeds when the marker already exists. `log` prints a full-access run's final output (or its stream) from the host, the same as `wait` does. `diff` does the same for full-access review runs; full-access build runs inspect commits.

Ref resolution runs outside the clone against the SSH URL from the repository argument. Remote Git uses an empty environment and explicit overrides for hooks, file monitoring, SSH, credentials, prompts, external protocols, paging, submodules, and automatic maintenance. It disables lazy fetching. Remote scripts use the C locale. Run file reads refuse symlinks, bound byte counts, check status fields, replace invalid UTF-8, and remove terminal control characters. Polls wait at least half a second between connections.

## Risk

The driver’s mirror receives agent-pushed objects.

One successful, real `agent exec` run on a host proves that harness. `proven_harnesses` records this fact without refusing unproven harnesses. An empty list never refuses a run. Remove the recorded fact when upgrading that harness. Codex uses the existing account executable on PATH and requires no standalone release layout.

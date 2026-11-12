---
description: Agent isolation constraints, coordination, and cleanup.
generated:
  - role: generated
    harness: codex
    model: openai:gpt-6
    at: "2026-11-12T03:35:46Z"
status: stable
tags: [alpha]
title: Agent isolation limits
type: Reference
---

# Agent isolation limits

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

## Risk

The taint record is per coordinator, and the host marker is a hint that the marked account can delete.

The driver refuses reviews on a host with no record until `clear-taint`.

The driver's mirror receives agent-pushed objects.

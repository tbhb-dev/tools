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
- `alpha` holds the interim agent tools that the tbhb-dev repositories used to carry in `tools/interim`. They may change or go away without notice.

## Agent SSH interface

`tbhbdev alpha agent ssh-serve` is the forced command for the `agent` account. Put the controller's public key in that account's `authorized_keys` behind this prefix, with `<agent home>` replaced by its absolute home path:

```text
restrict,command="<agent home>/.local/bin/tbhbdev alpha agent ssh-serve" ssh-ed25519 PUBLIC_KEY
```

Controllers select their key and turn off the identity agent:

```sh
ssh -i KEY -o IdentitiesOnly=yes -o IdentityAgent=none agent@HOST 'run-host NAME'
ssh -i KEY -o IdentitiesOnly=yes -o IdentityAgent=none agent@HOST 'result NAME'
ssh -i KEY -o IdentitiesOnly=yes -o IdentityAgent=none agent@HOST 'kill NAME'
```

On macOS, the controller uses `/Library/Application Support/tbhbdev/agent-launch/id_ed25519` (`ssh_serve.MACOS_LAUNCH_KEY`). Linux uses `/etc/tbhbdev/agent-launch/id_ed25519` (`ssh_serve.LINUX_LAUNCH_KEY`). Quote the macOS path when passing it to `-i`.

`run-host NAME` reads one JSON object from stdin, at most 1 MiB:

```json
{"v": 1, "request": {"v": 2}, "push_token": null}
```

Replace `request` with the complete existing run-host request for a public GitHub repository. Request and envelope versions are separate. Prepare `NAME.prompt` in the existing run state directory before connecting. This interface uses run-host's current prompt and configuration files. It creates `NAME.request.json` with mode `0600` under `$XDG_STATE_HOME/tbhbdev/agent-runs`, which defaults to `$HOME/.local/state/tbhbdev/agent-runs`. Each entry claims `NAME.owner` with exclusive creation before launch. An existing owner or request refuses a second run of that name.

Supply a short-lived push token as `push_token`, or `null` to skip pushing. One SSH connection remains open until the run finishes. It shows run-host's normal output. After the harness exits with a new commit on `agent/NAME`, the host pushes that branch over GitHub HTTPS. A one-shot credential helper reads the token through an inherited pipe. Tokens stay out of arguments, environment variables, config files, and run files. The host requires a nonempty private-name list and scans the outgoing commit messages and patches with the configured private-name rules before pushing. Push disables git hooks. A failed push records one error line and doesn't retry. After a failed harness, nothing pushes.

`result NAME` prints one JSON object with `v`, `name`, `rc`, `state`, `report`, `log_tail`, `branch`, `head`, `tools_commit` and `wheel_commit`. It reads the existing run files and limits the log to its last 200 lines. While a run continues, `rc` is `null`. Missing output fields use `null`. The `branch` field names only a branch the host pushed. `tools_commit` and `wheel_commit` identify the tools checkout and installed distribution, and absent provenance prints `unknown`. Run-host records both identities in its log header.

`kill NAME` stops the recorded process group after checking its identity. A supervisor records the run's completion. For a finished run it prints `already finished` and exits successfully.

| Exit code | Meaning |
| --- | --- |
| 0 | Successful command. |
| 2 | Refused command, account, envelope, or run data. |
| 3 | Unknown run name. |
| 4 | Branch push failed. |
| Other | The run-host exit status, including 1 for a refused run, 124 for a timeout, and 143 for an interrupted run. |

The GitHub credential (`gh`) is optional. Without it, build runs don't receive a GitHub token. The `signing` setting also accepts an absolute path to an account-owned OpenSSH key file with no group or world access. Sandbox rules deny access to that path.

## Documentation

- [Documentation index](docs/index.md)
- [Config](docs/reference/config.md)
- [Analytics](docs/reference/analytics.md)
- [Secrets](docs/reference/secrets.md)
- [Agent hosts](docs/reference/agent-hosts.md)
- [Plugins](docs/reference/plugins.md)
- [Alpha commands](docs/reference/alpha/commands.md)

## Layout

- `src/tbhbdev/` is the package. Each tool is a module there, registered in `cli.py`.
- `src/tbhbdev/alpha/` is the `alpha` package, one module for each alpha command.
- `tests/` holds the tests, one `test_<tool>.py` for each tool and `test_alpha_<command>.py` for each alpha command.

## Checks

Bazel runs every gate from tbhb-dev/rules: `bazelisk test //...`. See [AGENTS.md](AGENTS.md) for the setup and the workflow.

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

# Agent instructions

This is tbhb-dev/tools, the public repository of shared tools for tbhb-dev work. Every tool is a subcommand of the `tbhbdev` Python package.

## Standing rules

- Never write a public IP address in a commit, a pull request, or a report. Refer to the host by its name instead. The `public-ipv4` gitleaks rule enforces this.
- Secrets live in 1Password only. Never paste one into chat or commit one to git.
- A tool that one repository needs for its own checks or releases belongs in that repository or in tbhb-dev/rules, not here.

## Commits and pull requests

- Branch first. Every change lands through a pull request.
- Use Conventional Commits and a plain `git commit`, without `Signed-off-by` or `Co-authored-by`, and fill every section of `.github/PULL_REQUEST_TEMPLATE.md`.

## Adding a tool

1. Write the tool as `src/tbhbdev/<tool>.py`, or as a package directory under `src/tbhbdev/` for a larger one, with a `main(argv, prog)` that returns the exit status.
2. Register it in `TOOLS` in `src/tbhbdev/cli.py` under its command name.
3. Add `tests/test_<tool>.py`. The tests keep the package at 100% branch coverage, and mutation testing doesn't find a surviving mutant.
4. List it in the README.

## Alpha commands

`tbhbdev alpha` holds the interim agent tools, such as the agent identity hooks and the pull request commands `tbhbdev alpha pr` and `tbhbdev alpha merge`. They may change without notice. The harness hooks are user level, not in this repository: install `tbhbdev` with `uv tool install git+https://github.com/tbhb-dev/tools`, then run `tbhbdev alpha install` in a clone. See the README for what it writes.

## Checks

Run `mise install`, `uv sync` and `bazelisk run //:bazel_env`, which builds the `.bazel_env/bin` shims the git hooks call, then `tbhbdev alpha install`, which runs `prek install` and adds the identity hooks. Every gate is a Bazel test from tbhb-dev/rules, which `bazelisk test //...` runs, and `bazelisk run //:format` rewrites files into their canonical format. The pre-commit hooks run the same gates. To build against a local rules clone, put `common --override_module=tbhb_rules=<path to a rules clone>` and `common --override_module=tbhb_rules_python=<path to a rules clone>/python` in `.bazelrc.user`.

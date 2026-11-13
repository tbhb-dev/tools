"""`tbhbdev alpha`: interim agent tools, ported from an earlier interim toolset.

Alpha commands are interim. They may change or go away without notice, and
only the tbhb-dev harness hooks and agents in tbhb-dev repositories should
call them.

The harness hooks run on every tool call, so this module imports pr, review,
gh_app and merge, which load yaml, jsonschema and the Connect client, only when
one of them runs.
"""

import os
import sys
from typing import TYPE_CHECKING

from tbhbdev.alpha import (
    agent,
    brief,
    gh_shim,
    guard_gh_config_dir,
    guard_gh_poll,
    guard_gh_pr,
    guard_op,
    guard_scripts,
    identity,
    install,
    loop,
    pre_push_guard,
    session_context,
    state_guard,
    verify_range,
)

if TYPE_CHECKING:
    from collections.abc import Callable

__all__ = [
    "agent",
    "brief",
    "gh_shim",
    "guard_gh_config_dir",
    "guard_gh_poll",
    "guard_gh_pr",
    "guard_op",
    "guard_scripts",
    "identity",
    "install",
    "loop",
    "main",
    "pre_push_guard",
    "session_context",
    "state_guard",
    "verify_range",
]

USAGE = """usage: {prog} <command> [args...]

Alpha commands are interim: they may change or go away without notice.

commands:
  identity     agent identity: record <harness> | trailer MSGFILE [SOURCE]
               | consume | check MSGFILE | stamp-docs
  guard-gh-pr  PreToolUse hook: block gh pr create|edit|merge and
               a session's approval of a pull request it opened
  guard-op     PreToolUse hook: require the configured 1Password account
  state-guard  Claude Code hook: block status claims that carry no evidence
  session-context
               SessionStart hook: name the host, the other hosts and the time
  guard-gh-poll PreToolUse hook: block bulk GitHub reads and token unsets
  guard-gh-config-dir PreToolUse hook: require nonempty GH_CONFIG_DIR for gh
  guard-scripts PreToolUse hook: keep scripts for the operator in
               ~/.local/libexec/tbhb
  gh-shim      budget gh calls made by agent sessions and their scripts
  agent-env    launch a harness with the gh shim first on PATH
  pr           check | create | edit | hook: the pull request draft
               | backfill-opened OWNER: the opened ledgers
               | rerun N: rerun failed jobs for the current head
               | delete-branch N: delete a finished PR's head branch on origin
               | close N --reason TEXT: close a pull request (--comment is an alias)
  review       draft N [--force] | check N: the review draft
  issue        close N --reason completed|not-planned: close an issue
  gh-app       APP OWNER/REPO [--fresh] [--dry-run] [--all-repos]: mint a GitHub
               App installation token through Connect | APP --cooldown-until
               EPOCH | APP --cooldown-status
  repo         bootstrap [--dry-run] [--create --private|--public] ORG/REPO:
               apply the standard repository settings
  merge        squash-merge an approved pull request
  verify-range BASE TIP [-C PATH]: check every agent commit in a range
  loop         validate loop configuration (runner not yet available)
  agent        exec | wait | status | kill | clean | keep: headless runs
  pre-push-guard REMOTE URL: refuse rewritten history and private names
  brief        new | check: peer-run briefs with required safeguards
  worktree-prune [--apply] [--max-idle-days N] [--repo PATH]: remove finished
               worktrees under .worktrees (dry run by default)
  install      install the git hooks and the user-level harness hooks
"""


def identity_command(argv: list[str], _prog: str) -> int:
    return identity.run(argv, os.environ)


def guard_command(_argv: list[str], _prog: str) -> int:
    return guard_gh_pr.main()


def guard_op_command(_argv: list[str], _prog: str) -> int:
    return guard_op.main()


def state_guard_command(_argv: list[str], _prog: str) -> int:
    return state_guard.main()


def session_context_command(_argv: list[str], _prog: str) -> int:
    return session_context.main()


def poll_command(_argv: list[str], _prog: str) -> int:
    return guard_gh_poll.main()


def gh_config_dir_command(_argv: list[str], _prog: str) -> int:
    return guard_gh_config_dir.main()


def scripts_command(_argv: list[str], _prog: str) -> int:
    return guard_scripts.main()


def pr(argv: list[str], prog: str) -> int:
    # Every close goes through close.py's checks: agent session, open PR, ledger.
    if argv[:1] == ["close"]:
        from tbhbdev.alpha import close  # noqa: PLC0415 -- see the docstring

        return close.main(["pr", *argv], prog=prog)
    from tbhbdev.alpha import pull_request  # noqa: PLC0415 -- see the docstring

    return pull_request.main(argv, prog=prog)


def review_command(argv: list[str], prog: str) -> int:
    from tbhbdev.alpha import review  # noqa: PLC0415 -- see the docstring

    return review.main(argv, prog=prog)


def issue(argv: list[str], prog: str) -> int:
    from tbhbdev.alpha import close  # noqa: PLC0415 -- see the docstring

    return close.main(["issue", *argv], prog=prog)


def gh_app_command(argv: list[str], prog: str) -> int:
    from tbhbdev.alpha import gh_app  # noqa: PLC0415 -- see the docstring

    return gh_app.main(argv, prog)


def repo_command(argv: list[str], prog: str) -> int:
    from tbhbdev.alpha import repo_bootstrap  # noqa: PLC0415 -- see the docstring

    return repo_bootstrap.main(argv, prog=prog)


def worktree_prune_command(argv: list[str], prog: str) -> int:
    from tbhbdev.alpha import worktree_prune  # noqa: PLC0415 -- see the docstring

    return worktree_prune.main(argv, prog)


def merge(argv: list[str], prog: str) -> int:
    from tbhbdev.alpha import squash_merge  # noqa: PLC0415 -- see the docstring

    return squash_merge.main(argv, prog=prog)


def install_command(argv: list[str], prog: str) -> int:
    return install.main(argv, prog)


COMMANDS: dict[str, Callable[[list[str], str], int]] = {
    "identity": identity_command,
    "guard-gh-pr": guard_command,
    "guard-op": guard_op_command,
    "state-guard": state_guard_command,
    "session-context": session_context_command,
    "guard-gh-poll": poll_command,
    "guard-gh-config-dir": gh_config_dir_command,
    "guard-scripts": scripts_command,
    "gh-shim": gh_shim.main,
    "agent-env": gh_shim.agent_env,
    "pr": pr,
    "review": review_command,
    "issue": issue,
    "gh-app": gh_app_command,
    "repo": repo_command,
    "merge": merge,
    "worktree-prune": worktree_prune_command,
    "verify-range": verify_range.main,
    "install": install_command,
    "pre-push-guard": pre_push_guard.main,
    "loop": loop.main,
    "agent": agent.main,
    "brief": brief.main,
}


def main(argv: list[str], prog: str) -> int:
    """Run the alpha command argv names, passing it the rest of argv."""
    if not argv or argv[0] in {"-h", "--help"}:
        stream = sys.stdout if argv else sys.stderr
        stream.write(USAGE.format(prog=prog))
        return 0 if argv else 2
    command = COMMANDS.get(argv[0])
    if command is None:
        sys.stderr.write(f"{prog}: unknown command {argv[0]!r}\n")
        sys.stderr.write(USAGE.format(prog=prog))
        return 2
    return command(argv[1:], f"{prog} {argv[0]}")

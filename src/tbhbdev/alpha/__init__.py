"""`tbhbdev alpha`: interim agent tools, ported from ascm's tools/interim.

Alpha commands are interim. They may change or go away without notice, and
only the tbhb-dev harness hooks and agents in tbhb-dev repositories should
call them.

The harness hooks run on every tool call, so this module imports pr and merge,
which load yaml and jsonschema, only when one of them runs.
"""

import os
import sys
from typing import TYPE_CHECKING

from tbhbdev.alpha import (
    guard_gh_pr,
    identity,
    install,
    session_context,
    state_guard,
)

if TYPE_CHECKING:
    from collections.abc import Callable

__all__ = [
    "guard_gh_pr",
    "identity",
    "install",
    "main",
    "session_context",
    "state_guard",
]

USAGE = """usage: {prog} <command> [args...]

Alpha commands are interim: they may change or go away without notice.

commands:
  identity     agent identity: record <harness> | trailer MSGFILE [SOURCE]
               | consume | check MSGFILE | stamp-docs
  guard-gh-pr  PreToolUse hook: block gh pr create|edit|merge and
               a session's approval of a pull request it opened
  state-guard  Claude Code hook: block status claims that carry no evidence
  session-context
               SessionStart hook: name the host, the other hosts and the time
  pr           check | create | edit | hook: the pull request draft
               | backfill-opened OWNER: the opened ledgers
               | close N --reason TEXT: close a pull request
  issue        close N --reason completed|not-planned: close an issue
  merge        squash-merge an approved pull request
  install      install the git hooks and the user-level harness hooks
"""


def identity_command(argv: list[str], _prog: str) -> int:
    return identity.run(argv, os.environ)


def guard_command(_argv: list[str], _prog: str) -> int:
    return guard_gh_pr.main()


def state_guard_command(_argv: list[str], _prog: str) -> int:
    return state_guard.main()


def session_context_command(_argv: list[str], _prog: str) -> int:
    return session_context.main()


def pr(argv: list[str], prog: str) -> int:
    if argv[:1] == ["close"]:
        from tbhbdev.alpha import close  # noqa: PLC0415 -- see the docstring

        return close.main(["pr", *argv], prog=prog)
    from tbhbdev.alpha import pull_request  # noqa: PLC0415 -- see the docstring

    return pull_request.main(argv, prog=prog)


def issue(argv: list[str], prog: str) -> int:
    from tbhbdev.alpha import close  # noqa: PLC0415 -- see the docstring

    return close.main(["issue", *argv], prog=prog)


def merge(argv: list[str], prog: str) -> int:
    from tbhbdev.alpha import squash_merge  # noqa: PLC0415 -- see the docstring

    return squash_merge.main(argv, prog=prog)


def install_command(argv: list[str], prog: str) -> int:
    return install.main(argv, prog)


COMMANDS: dict[str, Callable[[list[str], str], int]] = {
    "identity": identity_command,
    "guard-gh-pr": guard_command,
    "state-guard": state_guard_command,
    "session-context": session_context_command,
    "pr": pr,
    "issue": issue,
    "merge": merge,
    "install": install_command,
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

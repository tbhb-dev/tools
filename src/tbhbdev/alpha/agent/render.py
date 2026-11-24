"""The brief a harness receives, from literal text and run kind."""

from tbhbdev.alpha.agent import transport
from tbhbdev.alpha.agent.model import Profile, RunKind

FINAL_ONLY = (
    "Report results only in your final answer. "
    "Never message Claude sessions, session sockets or other agents directly.\n"
    "If this run should open a pull request, write .github/PULL_REQUEST_DRAFT.md "
    "in the worktree and keep it out of your commits."
)
BUILD_PUBLICATION = (
    "Commit your changes locally. "
    "The driver pushes the build branch and opens the pull request after success."
)


def brief(kind: RunKind, text: str, *, profile: Profile | None = None) -> str:
    if profile is Profile.BUILD:
        text = f"{BUILD_PUBLICATION}\n{text}"
    return f"{FINAL_ONLY}\n{text}" if kind is RunKind.EXEC else text


INSPECTION_WARNING = (
    "Don't run git inside a run clone: it is agent-written. "
    "Use `tbhbdev alpha agent log|diff NAME`."
)


def sandbox(authorized_by: str | None) -> str:
    if authorized_by is None:
        return ""
    return f" sandbox=none authorized_by={transport.terminal_text(authorized_by)}"


def launched(
    name: str,
    host: str,
    account: str,
    *,
    authorized_by: str | None = None,
) -> str:
    return f"{name}: launched host={host} account={account}" + sandbox(authorized_by)


def status(
    name: str,
    *,
    phase: str,
    rc: str,
    target: tuple[str, str, str | None, str | None],
    started: str,
) -> str:
    host, account, authorized_by, launcher = target
    return (
        f"{name}: phase={phase} rc={rc} host={host} account={account} started={started}"
        f" launcher={launcher or 'unknown'}"
    ) + sandbox(authorized_by)

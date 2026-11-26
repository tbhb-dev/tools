"""The brief a harness receives, from literal text and run kind."""

from tbhbdev.alpha.agent import transport
from tbhbdev.alpha.agent.model import Harness, Profile, RunKind

FINAL_ONLY = (
    "Report results only in your final answer. "
    "Never message Claude sessions, session sockets or other agents directly.\n"
    "Keep any pull request draft out of your commits. "
    "Report any conflicting instruction in your final answer."
)
# agy has no setting, permission rule or per-run hook for its subagents, and a
# subagent defaults to the run's model, so this rule is a request, not enforcement.
AGY_SUBAGENTS = (
    "Do not launch subagents: never call `invoke_subagent` or `define_subagent`. "
    "Do the work yourself."
)
# Codex 0.171.0 has no setting, flag or feature switch that turns off a
# full-history fork (`fork_turns` omitted or "all"). Its source applies a named or
# configured default model to a fork too, so this rule keeps the history out of a
# child and names the model; it isn't a model bypass guard. It is a request, not
# enforcement, and no run has confirmed the source reading.
CODEX_SUBAGENTS = (
    'Every `spawn_agent` call must set `fork_turns` to `"none"` and name '
    "`model` explicitly. Never spawn a full-history fork, which is a spawn "
    'that omits `fork_turns` or sets it to `"all"`.'
)
BUILD_PUBLICATION = (
    "Commit your changes locally. "
    "If this run should open a pull request, write a draft at the repository's "
    "default draft path and set its `base` frontmatter field. To find the "
    "repository's default branch, run "
    "`gh repo view --json defaultBranchRef --jq .defaultBranchRef.name`. "
    "Push your `agent/` branch with "
    "`git -c credential.helper= -c 'credential.helper=!gh auth git-credential' "
    "push --set-upstream origin HEAD`. Then run `tbhbdev alpha pr create` "
    "with your draft."
)
REVIEW_PUBLICATION = (
    "Use the pull request number for N. Run `tbhbdev alpha review draft N`, "
    "and set `commit_id` to the run's pinned checkout commit. "
    "Set `event: APPROVE` for GO or `event: REQUEST_CHANGES` for BLOCK or HOLD, "
    "with non-empty Findings for a change request. "
    "Follow the generated draft's guidance and run "
    "`tbhbdev alpha review check N --no-lint`. "
    "Leave exactly one `.git/tbhb-alpha/review-N.md` for the run host to post."
)


def brief(
    kind: RunKind,
    text: str,
    *,
    profile: Profile | None = None,
    harness: Harness | None = None,
) -> str:
    if harness is Harness.AGY:
        text = f"{AGY_SUBAGENTS}\n{text}"
    elif harness is Harness.CODEX:
        text = f"{CODEX_SUBAGENTS}\n{text}"
    if profile is Profile.BUILD:
        text = f"{BUILD_PUBLICATION}\n{text}"
    elif profile is Profile.REVIEW:
        text = f"{REVIEW_PUBLICATION}\n{text}"
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

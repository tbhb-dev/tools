"""The brief a harness receives, from literal text and run kind."""

from tbhbdev.alpha.agent.model import RunKind

FINAL_ONLY = (
    "Report results only in your final answer. "
    "Never message Claude sessions, session sockets or other agents directly."
)


def brief(kind: RunKind, text: str) -> str:
    return f"{FINAL_ONLY}\n{text}" if kind is RunKind.EXEC else text

from tbhbdev.alpha.agent import transport

INSPECTION_WARNING = (
    "Don't run git inside a run clone: it is agent-written. "
    "Use `tbhbdev alpha agent log|diff NAME`."
)


def sandbox(authorized_by: str | None) -> str:
    if authorized_by is None:
        return ""
    return f" sandbox=none authorized_by={transport.terminal_text(authorized_by)}"


def trust(*, tainted_host: bool) -> str:
    return " untrusted-host" if tainted_host else ""


def launched(
    name: str,
    host: str,
    account: str,
    *,
    authorized_by: str | None = None,
    tainted_host: bool = False,
) -> str:
    return (
        f"{name}: launched host={host} account={account}"
        + sandbox(authorized_by)
        + trust(tainted_host=tainted_host)
    )


def status(
    name: str,
    *,
    phase: str,
    rc: str,
    target: tuple[str, str, str | None],
    started: str,
) -> str:
    host, account, authorized_by = target
    return (
        f"{name}: phase={phase} rc={rc} host={host} account={account} started={started}"
    ) + sandbox(authorized_by)

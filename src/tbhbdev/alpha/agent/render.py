"""The brief and coordinator reports."""

from tbhbdev.alpha.agent import transport

INSPECTION_WARNING = (
    "Don't run git inside a run clone: it is agent-written. "
    "Use `tbhbdev alpha agent log|diff NAME`."
)


def sandbox(authorized_by: str | None) -> str:
    if authorized_by is None:
        return ""
    return f" sandbox=none authorized_by={transport.terminal_text(authorized_by)}"


def launched(
    name: str, host: str, account: str, *, authorized_by: str | None = None
) -> str:
    return f"{name}: launched host={host} account={account}" + sandbox(authorized_by)


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

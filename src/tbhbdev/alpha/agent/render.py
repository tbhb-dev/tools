"""The brief and coordinator reports."""

from tbhbdev.alpha.agent import transport

INSPECTION_WARNING = (
    "Don't run git inside a run clone: it is agent-written. "
    "Use `tbhbdev alpha agent log|diff NAME`."
)


def launched(name: str, host: str, account: str) -> str:
    return f"{name}: launched host={host} account={account}"


def status(
    name: str, *, phase: str, rc: str, target: tuple[str, str], started: str
) -> str:
    host, account = target
    return (
        f"{name}: phase={phase} rc={rc} host={host} account={account} started={started}"
    )


def unsandboxed(name: str, authorization: str | None) -> str:
    return transport.terminal_text(
        f"{name}: sandbox=none authorized_by={authorization}"
    )

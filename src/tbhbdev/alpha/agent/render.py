"""The brief and coordinator reports."""


def launched(name: str, host: str, account: str) -> str:
    return f"{name}: launched host={host} account={account}"


def status(
    name: str, *, phase: str, rc: str, target: tuple[str, str], started: str
) -> str:
    host, account = target
    return (
        f"{name}: phase={phase} rc={rc} host={host} account={account} started={started}"
    )

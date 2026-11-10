"""The alpha agent command, including the hidden host-side runner."""

import sys

from tbhbdev import userconfig
from tbhbdev.alpha.agent import run_host as run_host
from tbhbdev.alpha.agent import settings

__all__ = ["main", "settings"]


def main(_argv: list[str], prog: str) -> int:
    """Dispatch the host supervisor or validate the agent settings."""
    if _argv and _argv[0] == "run-host":
        return run_host.main(_argv[1:], f"{prog} run-host")
    try:
        settings.load()
    except userconfig.ConfigError as exc:
        print(f"{prog}: {exc}", file=sys.stderr)
        return 1
    print(f"{prog}: configuration valid; runs are not implemented", file=sys.stderr)
    return 1

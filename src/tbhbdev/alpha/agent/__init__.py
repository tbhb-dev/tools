"""The alpha agent command, currently limited to configuration validation."""

import sys

from tbhbdev import userconfig
from tbhbdev.alpha.agent import settings

__all__ = ["main", "settings"]


def main(_argv: list[str], prog: str) -> int:
    """Validate settings and refuse to run until runs exist."""
    try:
        settings.load()
    except userconfig.ConfigError as exc:
        print(f"{prog}: {exc}", file=sys.stderr)
        return 1
    print(f"{prog}: configuration valid; runs are not implemented", file=sys.stderr)
    return 1

"""The alpha loop command, currently limited to configuration validation."""

import argparse
import sys

from tbhbdev import userconfig
from tbhbdev.alpha.loop import config

__all__ = ["config", "main"]


def main(argv: list[str], prog: str) -> int:
    """Validate settings and refuse to run until the shadow runner exists."""
    parser = argparse.ArgumentParser(
        prog=prog,
        description="Validate loop configuration; the runner is not available.",
    )
    parser.parse_args(argv)
    try:
        config.load()
    except userconfig.ConfigError as exc:
        print(f"{prog}: {exc}", file=sys.stderr)
        return 1
    print(
        f"{prog}: configuration valid; shadow runner is not implemented",
        file=sys.stderr,
    )
    return 1

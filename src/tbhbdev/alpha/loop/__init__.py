"""The alpha loop command, currently limited to configuration validation."""

import argparse
import sys

from tbhbdev import userconfig
from tbhbdev.alpha.loop import config

__all__ = ["config", "main"]


def main(argv: list[str], prog: str) -> int:
    """Validate settings and refuse to run until live intake adapters exist."""
    parser = argparse.ArgumentParser(
        prog=prog,
        description="Validate loop configuration; live intake is not wired yet.",
    )
    parser.parse_args(argv)
    try:
        config.load()
    except userconfig.ConfigError as exc:
        print(f"{prog}: {exc}", file=sys.stderr)
        return 1
    print(
        f"{prog}: configuration valid; shadow runner exists; "
        "live intake is not wired yet",
        file=sys.stderr,
    )
    return 1

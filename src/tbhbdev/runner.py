"""The main of a tool whose subcommands are argparse subparsers.

Each subparser sets `run` to its handler. A usage error exits 2, as argparse
does; the tool's own error prints as `PROG: message` and exits 1.
"""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import argparse
    from collections.abc import Callable


def main(
    parser: argparse.ArgumentParser,
    argv: list[str],
    errors: type[Exception],
) -> int:
    try:
        args = parser.parse_args(argv)
    except SystemExit as stop:
        return stop.code if isinstance(stop.code, int) else 2
    handler: Callable[[argparse.Namespace], int] = args.run
    try:
        return handler(args)
    except errors as exc:
        print(f"{parser.prog}: {exc}", file=sys.stderr)
        return 1

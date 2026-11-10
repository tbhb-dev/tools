"""Analytics migrations and contract checks."""

import argparse
import sys

from tbhbdev.analytics import catalog, database, render, settings
from tbhbdev.analytics.decisions.contract import matches

__all__ = ["catalog", "database", "main", "render", "settings"]


def main(argv: list[str], prog: str) -> int:
    parser = argparse.ArgumentParser(prog=prog)
    commands = parser.add_subparsers(dest="command", required=True)
    migrate = commands.add_parser("migrate")
    migrate.add_argument("--dry-run", action="store_true")
    contract = commands.add_parser("contract")
    contract.add_subparsers(required=True).add_parser("check")
    args = parser.parse_args(argv)
    try:
        options = settings.load()
        connection = database.connect(options)
        try:
            if args.command == "migrate":
                pending = database.migrate(connection, options, dry_run=args.dry_run)
                sys.stdout.write(render.migrated(pending, dry_run=args.dry_run))
                return 0
            verdict = matches(catalog.read(connection), catalog.contract())
            sys.stdout.write(render.checked(verdict))
            return int(bool(verdict.violations))
        finally:
            connection.close()
    except Exception as exc:  # noqa: BLE001
        sys.stderr.write(render.failed(exc))
        return 1

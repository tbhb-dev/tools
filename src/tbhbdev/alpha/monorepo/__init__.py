"""`tbhbdev alpha monorepo`: import a public repository into the monorepo.

`map` lists every file of a source commit as copied, dropped or rewritten.
Public-origin repositories only.
"""

import argparse
import sys
from pathlib import Path

from tbhbdev.alpha.agent.transport import terminal_text
from tbhbdev.alpha.monorepo import pathmap
from tbhbdev.alpha.monorepo.source import (
    MonorepoError,
    files,
    origin_repo,
    resolve,
)

EXIT_ERROR = 2


def make_map(args: argparse.Namespace) -> int:
    commit = resolve(args.clone, args.commit)
    repo = args.repo or origin_repo(args.clone)
    dest = pathmap.check_path(args.dest.rstrip("/"))
    made = pathmap.build(repo, commit, dest, list(files(args.clone, commit)))
    content = pathmap.dump(made)
    if args.output:
        Path(args.output).write_text(content, encoding="utf-8")
    else:
        sys.stdout.write(content)
    return 0


def parser(prog: str) -> argparse.ArgumentParser:
    top = argparse.ArgumentParser(prog=prog, description=__doc__)
    sub = top.add_subparsers(dest="command", required=True)
    one = sub.add_parser("map", help="write a path map for a source commit")
    one.add_argument("clone")
    one.add_argument("commit")
    one.add_argument("--dest", required=True, metavar="FOLDER")
    one.add_argument("--repo", metavar="OWNER/NAME")
    one.add_argument("-o", "--output", metavar="FILE")
    return top


COMMANDS = {"map": make_map}


def main(argv: list[str], prog: str) -> int:
    try:
        args = parser(prog).parse_args(argv)
    except SystemExit as exc:
        return int(exc.code or 0)
    try:
        return COMMANDS[args.command](args)
    except (MonorepoError, OSError, UnicodeError) as exc:
        print(f"{prog}: {terminal_text(str(exc))}", file=sys.stderr)
        return EXIT_ERROR

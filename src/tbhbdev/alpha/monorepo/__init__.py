"""`tbhbdev alpha monorepo`: import a public repository into the monorepo.

`map` lists every file of a source commit as copied, dropped or rewritten, and
`import` copies them into a monorepo clone as one commit. Public-origin
repositories only.
"""

import argparse
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from tbhbdev.alpha.agent.transport import terminal_text
from tbhbdev.alpha.monorepo import pathmap
from tbhbdev.alpha.monorepo.source import (
    EXECUTABLE,
    LINK,
    MonorepoError,
    files,
    git,
    origin_repo,
    resolve,
)

if TYPE_CHECKING:
    from collections.abc import Callable

SUBJECT_LIMIT = 72
EXIT_ERROR = 2


def source_and_map(source: str, map_file: str) -> tuple[str, str, pathmap.PathMap]:
    """The clone, the full commit, and the map, which must be for that commit.

    Raises:
        MonorepoError: ``source`` isn't ``CLONE@COMMIT`` or the map is for
            another commit.
    """
    clone, _, ref = source.rpartition("@")
    if not clone or not ref:
        message = f"{source!r} is not CLONE@COMMIT"
        raise MonorepoError(message)
    commit = resolve(clone, ref)
    made = pathmap.load(Path(map_file).read_text(encoding="utf-8"))
    if made.commit != commit:
        message = f"the map is for {made.commit}, not {commit}"
        raise MonorepoError(message)
    return clone, commit, made


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


def place(
    root: Path, folder: Path, dest: str, mode: str, data: bytes
) -> list[Callable[[], None]]:
    """Write one file or link and return how to undo it, folders first.

    Raises:
        MonorepoError: The path leaves ``folder``.
    """
    target = root / dest
    if not target.parent.resolve().is_relative_to(folder):
        message = f"{dest} would leave {folder.relative_to(root.resolve())}"
        raise MonorepoError(message)
    chain = [*reversed(target.parent.parents), target.parent]
    made = [d for d in chain if d.is_relative_to(root) and not d.exists()]
    target.parent.mkdir(parents=True, exist_ok=True)
    if mode == LINK:
        target.symlink_to(os.fsdecode(data))
    else:
        target.write_bytes(data)
        target.chmod(0o755 if mode == EXECUTABLE else 0o644)
    return [*(d.rmdir for d in made), target.unlink]


def import_files(args: argparse.Namespace) -> int:
    clone, commit, made = source_and_map(args.source, args.map)
    listing = files(clone, commit)
    if problems := made.gaps(listing):
        raise MonorepoError("\n".join(problems))
    into = args.into
    # Git runs from the clone's root, so the root-relative paths below are right
    # even when --into is a subfolder.
    top = git(into, "rev-parse", "--show-toplevel").decode().strip()
    root = Path(top)
    if git(top, "status", "--porcelain"):
        message = f"{into} has uncommitted changes"
        raise MonorepoError(message)
    targets = [dest for dest, _ in made.writes().values()]
    if clash := [d for d in targets if os.path.lexists(root / d)]:
        message = f"already in {into}: {', '.join(clash)}"
        raise MonorepoError(message)
    name = made.repo.partition("/")[2]
    subject = f"feat({made.dest}): import {name} at {commit[:7]}"
    if len(subject) > SUBJECT_LIMIT:
        message = f"the subject is over {SUBJECT_LIMIT} characters: {subject}"
        raise MonorepoError(message)
    body = (
        f"Source repository: {made.repo}\nSource commit: {commit}\n\n"
        f"Copied {len(made.copy)} files, rewrote {len(made.rewrite)} and "
        f"dropped {len(made.drop)}."
    )
    undo: list[Callable[[], None]] = []
    try:
        for _, dest, mode, data in pathmap.outputs(clone, listing, made):
            undo += place(root, root.resolve() / made.dest, dest, mode, data)
        paths = "\0".join(targets).encode()
        git(top, "add", "--pathspec-from-file=-", "--pathspec-file-nul", data=paths)
        git(top, "commit", "--quiet", "-F", "-", data=f"{subject}\n\n{body}\n".encode())
    except MonorepoError, OSError:
        # The clone was clean, so unstaging is a reset. Then delete what this run
        # wrote: files first, then the folders it created, deepest first.
        try:
            git(top, "reset", "--quiet")
        finally:
            for step in reversed(undo):
                step()
        raise
    print(f"imported {len(targets)} files from {made.repo} at {commit[:7]}")
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
    two = sub.add_parser("import", help="copy the files into a monorepo clone")
    two.add_argument("source", metavar="CLONE@COMMIT")
    two.add_argument("--map", required=True)
    two.add_argument("--into", required=True, metavar="MONOREPO_CLONE")
    return top


COMMANDS = {"map": make_map, "import": import_files}


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

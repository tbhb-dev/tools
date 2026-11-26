"""`tbhbdev alpha monorepo`: import a public repository into the monorepo.

`map` lists every file of a source commit as copied, dropped or rewritten, and
`import` copies them into a monorepo clone as one commit. Public-origin
repositories only.
"""

import argparse
import os
import sys
from pathlib import Path

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

SUBJECT_LIMIT = 72
EXIT_ERROR = 2


def split(source: str) -> tuple[str, str]:
    """``CLONE@COMMIT`` as its two halves.

    Raises:
        MonorepoError: There is no ``@`` with a clone before it and a commit after.
    """
    clone, _, ref = source.rpartition("@")
    if not clone or not ref:
        message = f"{source!r} is not CLONE@COMMIT"
        raise MonorepoError(message)
    return clone, ref


def source_and_map(source: str, map_file: str) -> tuple[str, str, pathmap.PathMap]:
    """The clone, the full commit, and the map, which must be for that commit."""
    clone, ref = split(source)
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


def place(root: Path, folder: Path, dest: str, mode: str, data: bytes) -> Path:
    """Write one file or link, refusing a path that leaves ``folder``."""
    target = root / dest
    if not target.parent.resolve().is_relative_to(folder):
        message = f"{dest} would leave {folder.relative_to(root.resolve())}"
        raise MonorepoError(message)
    target.parent.mkdir(parents=True, exist_ok=True)
    if mode == LINK:
        target.symlink_to(os.fsdecode(data))
    else:
        target.write_bytes(data)
        target.chmod(0o755 if mode == EXECUTABLE else 0o644)
    return target


def write_files(root: Path, made: pathmap.PathMap, clone: str, commit: str) -> int:
    """Write each file to the clone; on a failure, remove what was written."""
    folder = root.resolve() / made.dest
    written: list[Path] = []
    try:
        for _, dest, mode, data in pathmap.outputs(clone, files(clone, commit), made):
            written.append(place(root, folder, dest, mode, data))
    except MonorepoError, OSError:
        for target in written:
            target.unlink()
        raise
    return len(written)


def import_files(args: argparse.Namespace) -> int:
    clone, commit, made = source_and_map(args.source, args.map)
    if problems := made.gaps(files(clone, commit)):
        raise MonorepoError("\n".join(problems))
    into = args.into
    root = Path(git(into, "rev-parse", "--show-toplevel").decode().strip())
    if git(into, "status", "--porcelain"):
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
    count = write_files(root, made, clone, commit)
    git(
        into,
        "add",
        "--pathspec-from-file=-",
        "--pathspec-file-nul",
        data="\0".join(targets).encode(),
    )
    body = (
        f"Source repository: {made.repo}\nSource commit: {commit}\n\n"
        f"Copied {len(made.copy)} files, rewrote {len(made.rewrite)} and "
        f"dropped {len(made.drop)}."
    )
    git(into, "commit", "--quiet", "-F", "-", data=f"{subject}\n\n{body}\n".encode())
    print(f"imported {count} files from {made.repo} at {commit[:7]}")
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

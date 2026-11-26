"""Check an imported tree against its source commit and path map."""

import os
import re
import stat
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

from tbhbdev.alpha.monorepo.pathmap import rewrite_bytes
from tbhbdev.alpha.monorepo.source import (
    EXECUTABLE,
    LINK,
    MonorepoError,
    contents,
    files,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

    from tbhbdev.alpha.monorepo.pathmap import PathMap
    from tbhbdev.alpha.monorepo.source import Blob

# Prose that names an old repository is a dated record and stays as written.
PROSE = frozenset({".md", ".mdx", ".rst", ".txt", ".adoc"})
MODULE = re.compile(rb"^module\s+(\S+)", re.MULTILINE)
# Repositories in the design's "Where each repository goes" path map
# (wiki/designs/monorepo/C-layout-and-ownership.md). Standalone repos stay out.
OLD_REPOS = (
    rb"tbhb-dev/(?:agent-capacity|agent-config|agent-memories|ansible-collections|"
    rb"bazel-r2-cache|dashboard|design|dotfiles|evals|event-gateway|github-metrics|"
    rb"go\.tbhb\.dev|ops|pm|renovate-config|research|rules|runner-smoke|sm|spikes|"
    rb"telemetry|tools|wiki|work)"
)


def rules(repo: str, modules: set[str]) -> list[tuple[str, re.Pattern[bytes]]]:
    """The executable references to an old repository, as patterns.

    A workflow call or a Renovate preset from any repository of the owner is
    a reference, since the monorepo calls no old repository. Clone addresses
    name any repository in the design's path map or the source repository,
    which covers Bazel overrides, scripts and Go imports. Go module paths from
    the source's go.mod files add vanity paths.
    """
    owner, _ = (re.escape(part).encode() for part in repo.split("/"))
    addresses = OLD_REPOS + b"|" + re.escape(repo.encode())
    found = {
        "workflow uses an old repository": rb"\buses:\s*"
        rb"(?:[>|][-+]?[ \t]*(?:#[^\r\n]*)?\r?\n\s*)?['\"]?" + owner + rb"/",
        "Renovate preset from an old repository": rb"['\"](?:github|local)>"
        + owner
        + rb"/",
        "address of an old repository": rb"github\.com[/:](?:"
        + addresses
        + rb")(?![\w-])",
    }
    for module in sorted(modules):
        found[f"Go path {module}"] = (
            rb"(?<![\w.-])" + re.escape(module.encode()) + rb"(?![\w-])"
        )
    return [(rule, re.compile(pattern)) for rule, pattern in found.items()]


def references(
    dest: str, data: bytes, patterns: list[tuple[str, re.Pattern[bytes]]]
) -> Iterator[str]:
    """One finding for each line of a non-prose file that matches a rule."""
    if PurePosixPath(dest).suffix in PROSE:
        return
    found: dict[int, str] = {}
    for rule, pattern in patterns:
        for match in pattern.finditer(data):
            number = data.count(b"\n", 0, match.start()) + 1
            found.setdefault(number, rule)
    for number, rule in sorted(found.items()):
        yield f"{dest}:{number}: {rule}"


def go_modules(clone: str, listing: dict[str, Blob]) -> set[str]:
    """The module paths of the source's go.mod files."""
    mods = [src for src in listing if PurePosixPath(src).name == "go.mod"]
    return {
        match.decode()
        for data in contents(clone, [listing[src].oid for src in mods])
        for match in MODULE.findall(data)
    }


def read(path: Path) -> tuple[str, bytes] | None:
    """The git mode and bytes of the link or file at ``path``, if it is one.

    Raises:
        MonorepoError: The path can't be inspected or read.
    """
    try:
        mode = path.lstat().st_mode
        if stat.S_ISLNK(mode):
            return LINK, os.fsencode(path.readlink())
        if stat.S_ISREG(mode):
            git_mode = EXECUTABLE if mode & stat.S_IXUSR else "100644"
            return git_mode, path.read_bytes()
    except OSError as exc:
        message = f"{path}: cannot read: {exc}"
        raise MonorepoError(message) from exc
    return None


def inventory(tree: Path, dest: str, problems: list[str]) -> set[str]:
    """Files and links in the imported area, without following directory links.

    Append scan and stat errors to ``problems`` and continue with other paths.

    Raises:
        MonorepoError: A component leading to the imported area is a link.
        OSError: A component leading to the imported area can't be inspected.
    """
    folder = (tree / dest).absolute()
    for parent in (*reversed(folder.parents), folder):
        if stat.S_ISLNK(parent.lstat().st_mode):
            message = f"{parent}: symbolic link in destination path"
            raise MonorepoError(message)
    if stat.S_ISREG(folder.lstat().st_mode):
        return {dest}
    found: set[str] = set()
    pending = [folder]
    while pending:
        folder = pending.pop()
        try:
            children = list(folder.iterdir())
        except OSError as exc:
            problems.append(f"{folder}: cannot scan: {exc}")
            continue
        for child in children:
            try:
                mode = child.lstat().st_mode
            except OSError as exc:
                problems.append(f"{child}: cannot scan: {exc}")
                continue
            if stat.S_ISDIR(mode):
                pending.append(child)
                continue
            found.add(child.relative_to(tree.absolute()).as_posix())
    return found


def check(clone: str, commit: str, made: PathMap, tree: Path) -> list[str]:
    """Every problem found, one line each; none means the import is faithful."""
    listing = files(clone, commit)
    problems = made.gaps(listing)
    patterns = rules(made.repo, go_modules(clone, listing))
    writes = made.writes()
    try:
        actual = inventory(tree, made.dest, problems)
    except MonorepoError as exc:
        problems.append(str(exc))
        actual = set()
    except OSError as exc:
        problems.append(f"{tree / made.dest}: cannot scan: {exc}")
        actual = set()
    srcs = sorted(listing.keys() & writes.keys())
    expected = {writes[src][0] for src in srcs}
    problems.extend(
        f"{dest}: not produced by the map" for dest in sorted(actual - expected)
    )
    for src, data in zip(
        srcs, contents(clone, [listing[s].oid for s in srcs]), strict=True
    ):
        dest, entry = writes[src]
        mode = listing[src].mode
        try:
            wanted = rewrite_bytes(src, data, entry)
            got = read(tree / dest) if dest in actual else None
        except MonorepoError as exc:
            problems.append(str(exc))
            continue
        if got is None:
            problems.append(f"{dest}: missing from the tree")
        elif got[0] != mode:
            problems.append(f"{dest}: mode {got[0]}, source has {mode}")
        elif got[1] != wanted:
            problems.append(f"{dest}: differs from {src}")
        elif mode != LINK:
            problems.extend(references(dest, wanted, patterns))
    return problems

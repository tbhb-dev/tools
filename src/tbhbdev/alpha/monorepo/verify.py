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


def rules(repo: str, modules: set[str]) -> list[tuple[str, re.Pattern[bytes]]]:
    """The executable references to an old repository, as patterns.

    A workflow call or a Renovate preset from any repository of the owner is
    a reference, since the monorepo calls no old repository. A clone address
    names the source repository, which covers Bazel overrides, scripts and Go
    imports. Go module paths from the source's go.mod files add vanity paths.
    """
    owner, name = (re.escape(part).encode() for part in repo.split("/"))
    found = {
        "workflow uses an old repository": rb"\buses:\s*['\"]?" + owner + rb"/",
        "Renovate preset from an old repository": rb"['\"](?:github|local)>"
        + owner
        + rb"/",
        "address of the source repository": rb"github\.com[/:]"
        + owner
        + rb"/"
        + name
        + rb"(?![\w-])",
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
    for number, line in enumerate(data.split(b"\n"), 1):
        rule = next((rule for rule, p in patterns if p.search(line)), None)
        if rule:
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
    """The git mode and bytes of the link or file at ``path``, if it is one."""
    if path.is_symlink():
        return LINK, os.fsencode(path.readlink())
    if path.is_file():
        mode = EXECUTABLE if path.stat().st_mode & stat.S_IXUSR else "100644"
        return mode, path.read_bytes()
    return None


def inventory(tree: Path, dest: str) -> set[str]:
    """Files and links in the imported area, without following directory links.

    Raises:
        MonorepoError: A component leading to the imported area is a link.
    """
    folder = (tree / dest).absolute()
    for parent in (*reversed(folder.parents), folder):
        if parent.is_symlink():
            message = f"{parent}: symbolic link in destination path"
            raise MonorepoError(message)
    if folder.is_file():
        return {dest}
    return {
        (root / name).relative_to(tree.absolute()).as_posix()
        for root, _, names in folder.walk(follow_symlinks=False)
        for name in names
    }


def check(clone: str, commit: str, made: PathMap, tree: Path) -> list[str]:
    """Every problem found, one line each; none means the import is faithful."""
    listing = files(clone, commit)
    problems = made.gaps(listing)
    patterns = rules(made.repo, go_modules(clone, listing))
    writes = made.writes()
    try:
        actual = inventory(tree, made.dest)
    except MonorepoError as exc:
        problems.append(str(exc))
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
        except MonorepoError as exc:
            problems.append(str(exc))
            continue
        got = read(tree / dest) if dest in actual else None
        if got is None:
            problems.append(f"{dest}: missing from the tree")
        elif got[0] != mode:
            problems.append(f"{dest}: mode {got[0]}, source has {mode}")
        elif got[1] != wanted:
            problems.append(f"{dest}: differs from {src}")
        elif mode != LINK:
            problems.extend(references(dest, wanted, patterns))
    return problems

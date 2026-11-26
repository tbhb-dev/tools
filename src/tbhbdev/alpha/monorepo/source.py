"""Read a source clone at one commit, and run Git in a monorepo clone."""

import re
import subprocess  # nosec B404
from typing import TYPE_CHECKING, NamedTuple

if TYPE_CHECKING:
    from collections.abc import Iterator

# The source clone is downloaded data, so Git runs without its fsmonitor or
# pager settings. Paths are literal, because file names can hold glob magic.
SAFE = ("-c", "core.fsmonitor=false", "-c", "core.pager=cat", "--literal-pathspecs")
ORIGIN = re.compile(r"github\.com[:/]([^/:]+/[^/]+?)(?:\.git)?/?$")
BATCH = 100
# The git modes of a symbolic link and of an executable file.
LINK, EXECUTABLE = "120000", "100755"


class MonorepoError(Exception):
    """A usage, map or Git problem that stops the command."""


class Blob(NamedTuple):
    mode: str
    oid: str


def git(clone: str, *args: str, data: bytes | None = None) -> bytes:
    """Run Git in ``clone`` and return its stdout.

    Raises:
        MonorepoError: Git is missing or exits nonzero.
    """
    try:
        done = subprocess.run(  # nosec B603 B607
            ["git", *SAFE, "-C", clone, *args],
            input=data,
            capture_output=True,
            check=False,
        )
    except OSError as exc:
        raise MonorepoError(str(exc)) from exc
    if done.returncode:
        text = done.stderr.decode(errors="replace").strip()
        message = f"git {args[0]} failed in {clone}: {text}"
        raise MonorepoError(message)
    return done.stdout


def resolve(clone: str, ref: str) -> str:
    """The full object name of a commit."""
    return (
        git(clone, "rev-parse", "--verify", "--end-of-options", ref + "^{commit}")
        .decode()
        .strip()
    )


def origin_repo(clone: str) -> str:
    """The ``OWNER/NAME`` of the clone's origin on GitHub.

    Raises:
        MonorepoError: Origin is missing or isn't a GitHub address.
    """
    url = git(clone, "config", "--get", "remote.origin.url").decode().strip()
    found = ORIGIN.search(url)
    if not found:
        message = f"origin {url!r} is not a GitHub address; pass --repo OWNER/NAME"
        raise MonorepoError(message)
    return found.group(1)


def files(clone: str, commit: str) -> dict[str, Blob]:
    """Every path at the commit with its mode and object name.

    Raises:
        MonorepoError: A path isn't UTF-8, or the commit holds a submodule,
            which can't be copied.
    """
    listing = git(clone, "ls-tree", "-r", "-z", "--full-tree", commit)
    try:
        records = listing.decode().split("\0")[:-1]
    except UnicodeDecodeError as exc:
        message = "a path in the commit isn't UTF-8"
        raise MonorepoError(message) from exc
    found: dict[str, Blob] = {}
    for record in records:
        meta, _, path = record.partition("\t")
        mode, kind, oid = meta.split()
        if kind != "blob":
            message = f"{path} is a {kind}; submodules can't be imported"
            raise MonorepoError(message)
        found[path] = Blob(mode, oid)
    return found


def contents(clone: str, hashes: list[str]) -> Iterator[bytes]:
    """The bytes of each object, in order, read a batch at a time."""
    for start in range(0, len(hashes), BATCH):
        batch = hashes[start : start + BATCH]
        out = git(
            clone, "cat-file", "--batch", data="".join(f"{o}\n" for o in batch).encode()
        )
        at = 0
        for _ in batch:
            end = out.index(b"\n", at)
            size = int(out[at:end].split()[2])
            yield out[end + 1 : end + 1 + size]
            at = end + 2 + size

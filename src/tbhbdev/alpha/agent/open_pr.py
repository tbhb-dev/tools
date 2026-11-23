"""Open a run's pull request after scanning its worktree or committed draft."""

from __future__ import annotations

import os
import stat
import subprocess  # nosec B404
from contextlib import ExitStack
from typing import TYPE_CHECKING, NamedTuple, cast

import httpx

from tbhbdev import gitleaks_check
from tbhbdev.alpha import pull_request
from tbhbdev.alpha.agent import host_tools
from tbhbdev.alpha.agent.decisions import repo
from tbhbdev.alpha.agent.model import Invalid
from tbhbdev.host import GITLEAKS_EXTENDS

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

DRAFT_NAME = "PULL_REQUEST_DRAFT.md"
DRAFT = f".github/{DRAFT_NAME}"
DRAFT_LIMIT = 64 * 1024
TIMEOUT = httpx.Timeout(20.0, connect=5.0)


class OpenRefusedError(ValueError):
    """The draft or the API refused; the message is safe to log."""


class Draft(NamedTuple):
    """The parts of a draft that the API call needs."""

    title: str
    body: str
    base: str
    draft: bool


class PublishedRange(NamedTuple):
    """The run worktree and its pushed range in the host's Git metadata."""

    workspace: Path
    git_dir: Path
    before: str | None
    commit: str


def parse_draft(text: str) -> Draft:
    """Split a draft into its title, body and frontmatter fields.

    Only the frontmatter parser runs here. The schema, the section and prose
    checks and the Conventional Commit check read the repository or need tools
    that run hosts lack. The run checks those rules with ``pr check``.

    Raises:
        OpenRefusedError: The frontmatter is invalid or names no base, or the
            body doesn't start with an H1 title.
    """
    try:
        meta, body = pull_request.parse_frontmatter(text)
    except pull_request.FrontmatterError as exc:
        message = f"the draft frontmatter is invalid: {exc}"
        raise OpenRefusedError(message) from exc
    fields = cast("dict[str, object]", meta) if isinstance(meta, dict) else {}
    base = fields.get("base")
    if not isinstance(base, str) or not base:
        message = "the draft frontmatter names no base branch"
        raise OpenRefusedError(message)
    heading, _, rest = body.lstrip().partition("\n")
    title = heading.removeprefix("# ").strip()
    if not heading.startswith("# ") or not title:
        message = "the draft has no H1 title after its frontmatter"
        raise OpenRefusedError(message)
    return Draft(title, rest.strip("\n") + "\n", base, fields.get("draft") is True)


def read_worktree_draft(workspace: Path) -> bytes | None:
    """Read an owned draft through pinned, non-symlink directories."""
    try:
        with ExitStack() as stack:
            flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
            root = os.open(workspace, flags)
            stack.callback(os.close, root)
            try:
                directory = os.open(".github", flags, dir_fd=root)
                stack.callback(os.close, directory)
                fd = os.open(
                    DRAFT_NAME,
                    os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                    dir_fd=directory,
                )
            except FileNotFoundError:
                return None
            with os.fdopen(fd, "rb") as incoming:
                metadata = os.fstat(incoming.fileno())
                if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid():
                    message = "the worktree PR draft must be an owned regular file"
                    raise OpenRefusedError(message)
                data = incoming.read(DRAFT_LIMIT + 1)
                if len(data) > DRAFT_LIMIT:
                    message = "the worktree PR draft exceeds 64 KiB"
                    raise OpenRefusedError(message)
                return data
    except OSError as exc:
        message = "cannot read the worktree PR draft safely"
        raise OpenRefusedError(message) from exc


def publication_git(git_dir: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
    """Read Git objects using the host's protected publication repository."""
    return subprocess.run(  # nosec B603
        [host_tools.resolve("git"), "--git-dir", str(git_dir), *args],
        cwd=git_dir,
        env={
            "HOME": str(git_dir),
            "PATH": host_tools.search(),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
        },
        capture_output=True,
        check=False,  # pragma: no mutate -- Explicit subprocess default.
    )


def read_draft(published: PublishedRange) -> str:
    """Prefer the worktree draft and fall back when its path is absent."""
    data = read_worktree_draft(published.workspace)
    if data is None:
        done = publication_git(
            published.git_dir, "cat-file", "blob", f"{published.commit}:{DRAFT}"
        )
        if done.returncode:
            message = f"the worktree and pushed commit have no {DRAFT}"
            raise OpenRefusedError(message)
        data = done.stdout
    try:
        return data.decode()
    except UnicodeError as exc:
        message = "the PR draft must use UTF-8"
        raise OpenRefusedError(message) from exc


def check_range(published: PublishedRange, write: Callable[[str, str], None]) -> bytes:
    """Warn about draft additions and load the pre-run secret scan rules."""
    before = published.before
    if before is None:
        message = "cannot check the PR draft without a pre-run commit"
        raise OpenRefusedError(message)
    flags = "--no-ext-diff --no-textconv --no-renames --diff-filter=A --name-only"
    changed = publication_git(
        published.git_dir, "diff", *flags.split(), before, published.commit, "--", DRAFT
    )
    if changed.returncode:
        message = "cannot check whether the pushed range adds the PR draft"
        raise OpenRefusedError(message)
    if DRAFT.encode() in changed.stdout.splitlines():
        write("log", "draft committed: it will merge into main; remove it\n")
    rules = publication_git(
        published.git_dir, "cat-file", "blob", f"{before}:{GITLEAKS_EXTENDS}"
    )
    if rules.returncode:
        message = (
            f"cannot scan the PR draft without {GITLEAKS_EXTENDS} in the pre-run commit"
        )
        raise OpenRefusedError(message)
    return rules.stdout


def scan_draft(text: str, git_dir: Path, rules: bytes) -> None:
    """Scan the draft with the host's private rules and the pre-run Git policy."""
    (git_dir / GITLEAKS_EXTENDS).write_bytes(rules)
    try:
        gitleaks_check.scan_text(
            text,
            "PR draft",
            root=git_dir,
            gitleaks=host_tools.resolve("gitleaks"),
            visibility="",
        )
    except (gitleaks_check.ScanRefused, Invalid, UnicodeError) as exc:
        cause = (
            exc.locations
            if isinstance(exc, gitleaks_check.ScanRefused) and exc.locations
            else "check the draft and scanner configuration"
        )
        message = f"PR draft secret scan refused: {cause}"
        raise OpenRefusedError(message) from exc


def open_pull_request(
    published: PublishedRange,
    repository: str,
    branch: str,
    token: str,
    write: Callable[[str, str], None],
) -> int:
    """Create the PR and return its number.

    Raises:
        OpenRefusedError: The draft is missing or malformed, or GitHub
            refuses or cannot be reached.
    """
    rules = check_range(published, write)
    text = read_draft(published)
    scan_draft(text, published.git_dir, rules)
    draft = parse_draft(text.replace("\r\n", "\n").replace("\r", "\n"))
    _, owner, name = repo.parts(repository)
    try:
        response = httpx.post(
            f"https://api.github.com/repos/{owner}/{name}/pulls",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
            },
            json={
                "title": draft.title,
                "body": draft.body,
                "head": branch,
                "base": draft.base,
                "draft": draft.draft,
            },
            timeout=TIMEOUT,
        )
        result = (
            response.json() if response.status_code == httpx.codes.CREATED else None
        )
    except (httpx.HTTPError, ValueError) as exc:
        message = f"the PR request failed: {type(exc).__name__}"
        raise OpenRefusedError(message) from exc
    fields = cast("dict[str, int]", result)  # pragma: no mutate -- Cast only.
    if isinstance(result, dict) and isinstance(number := fields.get("number"), int):
        return number
    if response.status_code == httpx.codes.UNAUTHORIZED:
        message = f"GitHub rejected the PR token with status {response.status_code}"
        raise OpenRefusedError(message)
    message = f"GitHub refused the PR with status {response.status_code}"
    raise OpenRefusedError(message)

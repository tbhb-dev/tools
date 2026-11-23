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


def parse_draft(text: str, git_dir: Path | None = None) -> Draft:
    """Split a draft into its title, body and frontmatter fields.

    Only the frontmatter parser runs here. The schema, the section and prose
    checks and the Conventional Commit check read the repository or need tools
    that run hosts lack. The run checks those rules with ``pr check``.

    Raises:
        OpenRefusedError: The frontmatter is invalid or names a bad base, or the
            body doesn't start with an H1 title.
    """
    meta: object = None
    body = ""
    invalid = False
    try:
        meta, body = pull_request.parse_frontmatter(text)
    except pull_request.FrontmatterError:
        # The parser's text can quote draft lines, so the log gets only the
        # fact, and the error is raised outside the handler to drop its context.
        invalid = True
    if invalid:
        message = "the draft frontmatter is invalid"
        raise OpenRefusedError(message)
    fields = cast("dict[str, object]", meta) if isinstance(meta, dict) else {}
    # A missing base takes the repository's default branch, as `alpha pr` does.
    # The schema fallback exits when the working directory has no baseline.
    base = fields.get("base")
    if "base" not in fields:
        try:
            base = pull_request.default_base(git_dir)
        except SystemExit:
            message = "the draft names no base and no default branch is readable"
            raise OpenRefusedError(message) from None
    if not isinstance(base, str) or not base:
        message = "the draft frontmatter names an invalid base branch"
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


# Fixed phrases for scanner failures, chosen by the refusal's kind, so the log
# names the cause without repeating the scanner's own text.
SCAN_CAUSES = {
    "config": "the private gitleaks config or the extends file is unusable",
    "scanner_missing": "the gitleaks executable cannot run",
    "signal": "gitleaks was stopped by a signal",
}


def scan_cause(exc: Exception) -> str:
    """Name why the draft scan refused, from fixed phrases and integers only."""
    if isinstance(exc, gitleaks_check.ScanRefused):
        if exc.kind == "finding":
            lines = ", ".join(str(line) for line in exc.lines)
            return f"gitleaks finds a secret on lines: {lines}"
        if exc.kind == "exit" and exc.status is not None:
            return f"gitleaks failed (exit {exc.status})"
        return SCAN_CAUSES.get(
            exc.kind, "the scanner refused for a reason the log doesn't name"
        )
    if isinstance(exc, Invalid):
        # host_tools.resolve raises Invalid only when gitleaks is missing or a shim.
        return SCAN_CAUSES["scanner_missing"]
    return f"{type(exc).__name__} while scanning the draft"


def repository_visibility(
    repository: str, token: str, write: Callable[[str, str], None]
) -> str:
    """The visibility GitHub reports for REPOSITORY, or "" when it can't say.

    The scan applies the name rules to any value but ``PRIVATE``, so an
    unreadable, mismatched or unexpected answer scans the draft as public text
    and logs why. Only the exact value ``private`` for the named repository
    maps to ``PRIVATE``.
    """
    _, owner, name = repo.parts(repository)
    try:
        response = httpx.get(
            f"https://api.github.com/repos/{owner}/{name}",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
            },
            timeout=TIMEOUT,
        )
        data = response.json() if response.status_code == httpx.codes.OK else None
    except httpx.HTTPError, ValueError:
        write("log", "visibility lookup failed: unreachable or unreadable\n")
        return ""
    if not isinstance(data, dict):
        write("log", "visibility lookup failed: no usable answer\n")
        return ""
    full_name = data.get("full_name")
    if not isinstance(full_name, str) or full_name.lower() != f"{owner}/{name}".lower():
        write("log", "visibility lookup failed: answer names another repository\n")
        return ""
    value = data.get("visibility")
    if value == "private":
        return "PRIVATE"
    if isinstance(value, str) and value in ("public", "internal"):
        return value.upper()
    return ""


def scan_draft(text: str, git_dir: Path, rules: bytes, visibility: str) -> None:
    """Scan the draft with the host's private rules and the pre-run Git policy.

    VISIBILITY is that of the repository the PR opens in, as GitHub reports it.

    Raises:
        OpenRefusedError: The scan found a secret or could not run. It is
            raised outside the handler, so its ``__context__`` is None and
            the scanner's text and rule IDs never reach a traceback.
    """
    (git_dir / GITLEAKS_EXTENDS).write_bytes(rules)
    failure: Exception | None = None
    try:
        gitleaks_check.scan_text(
            text,
            "PR draft",
            root=git_dir,
            gitleaks=host_tools.resolve("gitleaks"),
            visibility=visibility,
        )
    except (gitleaks_check.ScanRefused, Invalid, UnicodeError) as exc:
        failure = exc
    if failure is not None:
        message = f"PR draft secret scan refused: {scan_cause(failure)}"
        raise OpenRefusedError(message)


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
    scan_draft(
        text, published.git_dir, rules, repository_visibility(repository, token, write)
    )
    draft = parse_draft(
        text.replace("\r\n", "\n").replace("\r", "\n"), published.git_dir
    )
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

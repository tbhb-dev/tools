"""Remove finished worktrees from a clone's `.worktrees` directory.

A worktree goes only when its branch is finished and nothing in it is at risk.
Finished means one of two things. Its head is already in origin/main and the
worktree has sat untouched for the idle limit, or a pull request from the
branch is merged and that pull request's head is the worktree's head. The
merged state comes from the pull request, because a squash merge leaves the
branch's own commits out of main and `git branch --merged` can't see it.

Nothing goes when the main checkout, a path outside `.worktrees`, a locked
worktree, a detached head, uncommitted or untracked work, the current
directory or any unreadable state is involved. Git's own `worktree remove`
runs without `--force`, so it refuses a dirty tree as well. A dry run is the
default. Before a removal the command asks Bazel in that worktree to shut down;
`bazel-prune` clears a leftover output base later.
"""

import argparse
import json
import os
import shutil
import subprocess  # nosec B404
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from tbhbdev.alpha.guard_gh_pr import origin_repo
from tbhbdev.alpha.repo import git

DAY = 86400
GH_TIMEOUT = 60
BAZEL_TIMEOUT = 60
DEFAULT_IDLE_DAYS = 14
BLOCK_BYTES = 512
DEFAULT_BRANCHES = ("main", "master")
HEAD_LENGTH = 40


@dataclass(frozen=True)
class Worktree:
    path: Path
    head: str
    branch: str | None
    locked: bool
    prunable: bool


@dataclass(frozen=True)
class Policy:
    root: Path
    cwd: Path
    slug: str | None
    days: int
    now: float


@dataclass(frozen=True)
class Result:
    path: Path
    action: str
    reason: str
    size_bytes: int


def parse_worktrees(text: str) -> list[Worktree]:
    """Read `git worktree list --porcelain -z`, one record per worktree."""
    found: list[Worktree] = []
    for record in text.split("\0\0"):
        fields = [field for field in record.split("\0") if field]
        if not fields:
            continue
        path = ""
        head = ""
        branch = None
        for field in fields:
            key, _, value = field.partition(" ")
            if key == "worktree":
                path = value
            elif key == "HEAD":
                head = value
            elif key == "branch":
                branch = value.removeprefix("refs/heads/")
        found.append(
            Worktree(
                Path(path),
                head,
                branch,
                locked=any(field.split(" ")[0] == "locked" for field in fields),
                prunable=any(field.split(" ")[0] == "prunable" for field in fields),
            )
        )
    return found


def list_worktrees(repo: Path) -> list[Worktree] | None:
    code, out = git("worktree", "list", "--porcelain", "-z", cwd=repo)
    if code or not out:
        return None
    return parse_worktrees(out)


def is_dirty(path: Path) -> bool | None:
    """Return whether the tree has uncommitted or untracked work, None if unreadable."""
    code, out = git("status", "--porcelain", "--untracked-files=all", cwd=path)
    if code:
        return None
    return bool(out)


def head_in_main(path: Path, head: str) -> bool | None:
    """Return whether the head is in origin/main, None if git can't say."""
    code, _ = git("merge-base", "--is-ancestor", head, "origin/main", cwd=path)
    if code == 0:
        return True
    return False if code == 1 else None


def on_a_remote(path: Path, head: str) -> bool | None:
    code, out = git(
        "for-each-ref", "--contains", head, "--format=%(refname)", "refs/remotes",
        cwd=path,
    )  # fmt: skip
    return bool(out) if code == 0 else None


def last_activity(path: Path, head: str) -> float | None:
    """Return the newest of the head's commit time and the tree's own change times."""
    code, stamp = git("log", "-1", "--format=%ct", head, cwd=path)
    code_dir, gitdir = git("rev-parse", "--absolute-git-dir", cwd=path)
    if code or code_dir:
        return None
    try:
        newest = float(stamp)
        newest = max(newest, path.stat().st_mtime)
        index = Path(gitdir) / "index"
        if index.exists():
            newest = max(newest, index.stat().st_mtime)
    except OSError, ValueError:
        return None
    return newest


def merged_pull_request(slug: str, branch: str, head: str) -> bool | None:
    """Return whether a merged pull request from this repository ends at `head`.

    One `gh pr list` call reads the branch's merged pull requests. None means
    the state couldn't be read.
    """
    tool = shutil.which("gh")
    if tool is None:
        return None
    try:
        done = subprocess.run(  # nosec B603
            [
                tool,
                "pr",
                "list",
                "--repo",
                f"github.com/{slug}",
                "--head",
                branch,
                "--state",
                "merged",
                "--json",
                "headRefOid,isCrossRepository",
                "--limit",
                "100",
            ],
            capture_output=True,
            text=True,
            check=False,
            input="",
            timeout=GH_TIMEOUT,
        )
        data = json.loads(done.stdout)
    except OSError, subprocess.TimeoutExpired, ValueError:
        return None
    if done.returncode or not isinstance(data, list):
        return None
    return any(
        isinstance(item, dict)
        and item.get("headRefOid") == head
        and item.get("isCrossRepository") is False
        for item in cast("list[object]", data)
    )


def keep_reason(wt: Worktree, root: Path, cwd: Path) -> str | None:
    """Return why a worktree stays, from facts that need no git read, or None."""
    resolved = wt.path.resolve()
    reason = None
    if wt.path.parent.resolve() != root:
        reason = "not directly under .worktrees"
    elif wt.prunable:
        reason = "directory missing"
    elif wt.locked:
        reason = "locked"
    elif wt.branch is None:
        reason = "detached head"
    elif wt.branch in DEFAULT_BRANCHES:
        reason = "default branch"
    elif cwd == resolved or resolved in cwd.parents:
        reason = "holds the working directory"
    elif len(wt.head) != HEAD_LENGTH:
        reason = "head unreadable"
    return reason


def idle_verdict(wt: Worktree, days: int, now: float) -> tuple[str, str]:
    """Judge a head that origin/main already holds by how long the tree sat idle."""
    seen = last_activity(wt.path, wt.head)
    if seen is None:
        return "keep", "git state unreadable"
    if now - seen < days * DAY:
        return "keep", f"active within {days} days"
    return "remove", f"no commits beyond origin/main, idle {days} days"


def pr_verdict(wt: Worktree, slug: str | None) -> tuple[str, str]:
    """Judge a head beyond origin/main by its branch's merged pull request."""
    if slug is None:
        return "keep", "origin unreadable"
    merged = merged_pull_request(slug, wt.branch or "", wt.head)
    if merged is None:
        return "keep", "pull request state unreadable"
    if merged:
        return "remove", "pull request merged"
    remote = on_a_remote(wt.path, wt.head)
    if remote is None:
        return "keep", "git state unreadable"
    return "keep", "not merged" if remote else "unpushed commits"


def finished(wt: Worktree, policy: Policy) -> tuple[str, str]:
    """Decide a clean, unlocked worktree: ("remove" | "keep", reason)."""
    in_main = head_in_main(wt.path, wt.head)
    if in_main is None:
        return "keep", "git state unreadable"
    if in_main:
        return idle_verdict(wt, policy.days, policy.now)
    return pr_verdict(wt, policy.slug)


def decide(wt: Worktree, policy: Policy) -> tuple[str, str]:
    reason = keep_reason(wt, policy.root, policy.cwd)
    if reason:
        return "keep", reason
    dirty = is_dirty(wt.path)
    if dirty is None:
        return "keep", "git state unreadable"
    if dirty:
        return "keep", "uncommitted or untracked work"
    return finished(wt, policy)


def tree_bytes(path: Path) -> int:
    """Count the allocated bytes of a tree without following links."""
    total = 0
    for directory, _, names in os.walk(path):
        total += sum(
            os.lstat(Path(directory, name)).st_blocks * BLOCK_BYTES for name in names
        )
    return total


def shutdown_bazel(path: Path) -> None:
    """Ask the worktree's Bazel server to stop; a failure leaves it for bazel-prune."""
    tool = shutil.which("bazelisk")
    if tool is None:
        return
    try:
        subprocess.run(  # nosec B603
            [tool, "shutdown"],
            cwd=path,
            check=False,
            timeout=BAZEL_TIMEOUT,
            capture_output=True,
        )
    except OSError, subprocess.TimeoutExpired:
        return


def remove(repo: Path, wt: Worktree) -> tuple[str, str]:
    shutdown_bazel(wt.path)
    code, _ = git("worktree", "remove", str(wt.path), cwd=repo)
    if code:
        return "keep", "git refused the removal"
    return "removed", "removed"


def run(
    repo: Path, days: int, *, apply: bool, now: float | None = None
) -> list[Result]:
    """Judge every worktree under the clone's `.worktrees`; remove if `apply`."""
    worktrees = list_worktrees(repo)
    if worktrees is None:
        message = "cannot read the worktree list"
        raise ValueError(message)
    main = worktrees[0].path
    policy = Policy(
        (main / ".worktrees").resolve(),
        Path.cwd().resolve(),
        origin_repo(main),
        days,
        time.time() if now is None else now,
    )
    results: list[Result] = []
    for wt in worktrees[1:]:
        action, reason = decide(wt, policy)
        size = tree_bytes(wt.path) if action == "remove" else 0
        if action == "remove" and apply:
            action, reason = remove(repo, wt)
        elif action == "remove":
            action = "would remove"
        results.append(Result(wt.path, action, reason, size))
    return results


def report(results: list[Result]) -> None:
    for item in results:
        print(f"{item.action}: {item.path}: {item.reason}")
    freed = sum(
        r.size_bytes for r in results if r.action in {"removed", "would remove"}
    )
    verb = "freed" if any(r.action == "removed" for r in results) else "would free"
    print(f"{verb} {freed} bytes")


def main(argv: list[str], prog: str) -> int:
    parser = argparse.ArgumentParser(prog=prog, description=__doc__)
    parser.add_argument("--apply", action="store_true", help="remove, not just report")
    parser.add_argument("--max-idle-days", type=int, default=DEFAULT_IDLE_DAYS)
    parser.add_argument("--repo", default=".", help="a worktree of the clone")
    args = parser.parse_args(argv)
    if args.max_idle_days < 0:
        print(f"{prog}: --max-idle-days must be nonnegative", file=sys.stderr)
        return 2
    try:
        results = run(Path(args.repo), args.max_idle_days, apply=args.apply)
    except ValueError as exc:
        print(f"{prog}: {exc}", file=sys.stderr)
        return 2
    report(results)
    return int(any(r.reason == "git refused the removal" for r in results))

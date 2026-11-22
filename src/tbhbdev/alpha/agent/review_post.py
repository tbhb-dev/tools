"""Post a review run's verdict as a GitHub review, host-side, with the run's token.

The sandbox only writes the draft file. Run-host validates it, adds the run's
identity to the body and posts it after the run ends. A run can comment or
request changes; approval stays with a human-gated reviewer. `review create`
is the same step as a command, with the token on stdin.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess  # nosec B404
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, cast

import httpx
from jsonschema import Draft202012Validator

from tbhbdev.alpha import pull_request, review, review_policy
from tbhbdev.alpha.agent import credentials, host_tools, runfiles
from tbhbdev.alpha.agent.decisions import repo

if TYPE_CHECKING:
    from tbhbdev.alpha.agent.run_host import Prepared, Run

ALLOWED_EVENTS = frozenset({"COMMENT", "REQUEST_CHANGES"})
DRAFT_NAME = re.compile(r"review-(\d+)\.md")
DRAFT_LIMIT = 64 * 1024
PAGE = 100
MAX_PAGES = 20
TIMEOUT = httpx.Timeout(20.0, connect=5.0)
POLICY_FILES = (
    review_policy.TOML,
    review_policy.TEMPLATE,
    review_policy.CHECKLIST,
    review_policy.DRAFT_SCHEMA,
)


class PostRefusedError(ValueError):
    """The draft or the API refused; the message is safe to log."""


@dataclass(frozen=True)
class Submission:
    """One review to post: the draft text and what the post must match."""

    repository: str
    number: int
    text: str
    commit: str
    stamp: str


def find_draft(workspace: Path) -> tuple[int, str]:
    """The run's one review draft as (pull request number, text).

    Raises:
        PostRefusedError: The run left no draft, or more than one.
    """
    directory = workspace / ".git" / "tbhb-alpha"
    found = sorted(
        (int(match[1]), path)
        for path in directory.glob("review-*.md")
        if (match := DRAFT_NAME.fullmatch(path.name))
    )
    if len(found) != 1:
        message = f"expected one review draft, found {len(found)}"
        raise PostRefusedError(message)
    number, path = found[0]
    return number, runfiles.read_owned(path, DRAFT_LIMIT, "review draft")


def validate(sub: Submission) -> tuple[dict[str, object], str]:
    """Check the draft host-side and return (frontmatter, body).

    The prose linters aren't on run hosts, so the run's own `review check`
    stands for them. Everything the schema, the template sections and the
    frontmatter's key uniqueness cover is checked again here.

    Raises:
        PostRefusedError: The draft fails a check, names another head than the
            run reviewed, or asks for an event a run can't post.
    """
    problems = review.check_text(sub.text, sub.number, lint=False)
    if problems:
        line, why = problems[0]
        message = f"the draft fails its checks: line {line}: {why}"
        raise PostRefusedError(message)
    meta, body = pull_request.parse_frontmatter(sub.text)
    fields = cast("dict[str, object]", meta)
    if fields["event"] not in ALLOWED_EVENTS:
        message = f"a run can't post a {fields['event']} review"
        raise PostRefusedError(message)
    if str(fields["commit_id"]).lower() != sub.commit.lower():
        message = "the draft's commit_id isn't the commit the run reviewed"
        raise PostRefusedError(message)
    return fields, body.strip("\n") + "\n"


def git_output(git_dir: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run git on one repository with no user or system configuration."""
    return subprocess.run(  # nosec B603
        [host_tools.resolve("git"), "--git-dir", str(git_dir), *args],
        env={
            "HOME": str(git_dir),
            "PATH": host_tools.search(),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
        },
        capture_output=True,
        text=True,
        check=False,
    )


def judged_policy(git_dir: Path, commit: str, into: Path) -> None:
    """Copy the review policy files the judged commit holds into `into`.

    The run's working tree is writable by the sandbox, so policy comes from
    the commit, which git addresses by content.

    Raises:
        PostRefusedError: Git can't read the commit.
    """
    for path in POLICY_FILES:
        listed = git_output(git_dir, "ls-tree", "--name-only", commit, "--", str(path))
        if listed.returncode:
            message = "cannot read the judged commit"
            raise PostRefusedError(message)
        if not listed.stdout.strip():
            continue
        shown = git_output(git_dir, "show", f"{commit}:{path}")
        if shown.returncode:
            message = f"cannot read {path} from the judged commit"
            raise PostRefusedError(message)
        (into / path).parent.mkdir(parents=True, exist_ok=True)
        (into / path).write_text(shown.stdout, encoding="utf-8")


def check_policy(sub: Submission, fields: dict[str, object], git_dir: Path) -> None:
    """Hold the draft to the policy files of the judged commit.

    Raises:
        PostRefusedError: A policy file has findings, or the repository's
            draft schema rejects the draft.
    """
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        judged_policy(git_dir, sub.commit, root)
        findings = review_policy.check(root)
        if findings:
            first = findings[0]
            message = f"{first.path}: {first.rule}: {first.message}"
            raise PostRefusedError(message)
        schema_file = root / review_policy.DRAFT_SCHEMA
        if schema_file.exists():
            schema = json.loads(schema_file.read_text(encoding="utf-8"))
            errors = list(Draft202012Validator(schema).iter_errors(fields))
            if errors:
                message = f"the draft schema rejects it: {errors[0].message}"
                raise PostRefusedError(message)


def api(
    method: str, url: str, token: str, payload: dict[str, str] | None = None
) -> object:
    """One GitHub API call; any 2xx is success.

    Raises:
        PostRefusedError: GitHub refuses, can't be reached, or sends no JSON.
    """
    try:
        response = httpx.request(
            method,
            url,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
            },
            json=payload,
            timeout=TIMEOUT,
        )
        data = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        message = f"the {method} request failed: {type(exc).__name__}"
        raise PostRefusedError(message) from exc
    if not response.is_success:
        message = f"GitHub refused the {method} with status {response.status_code}"
        raise PostRefusedError(message)
    return data


def pull_head(data: object) -> str | None:
    head = data.get("head") if isinstance(data, dict) else None
    sha = head.get("sha") if isinstance(head, dict) else None
    return sha if isinstance(sha, str) else None


def existing_review(base: str, token: str, stamp: str) -> int | None:
    """The id of a review that already carries this run's stamp, if any.

    Raises:
        PostRefusedError: GitHub sends no list, or too many pages to read.
    """
    for page in range(1, MAX_PAGES + 1):
        data = api("GET", f"{base}/reviews?per_page={PAGE}&page={page}", token)
        if not isinstance(data, list):
            message = "GitHub sent no review list"
            raise PostRefusedError(message)
        for item in cast("list[object]", data):
            entry = cast("dict[str, object]", item) if isinstance(item, dict) else {}
            found = entry.get("id")
            if isinstance(found, int) and stamp in str(entry.get("body")):
                return found
        if len(data) < PAGE:
            return None
    message = "too many reviews to check for a duplicate"
    raise PostRefusedError(message)


def create(sub: Submission, token: str, git_dir: Path) -> int:
    """Validate, then post once; return the review id.

    A repeat of the same run's post returns the existing review's id.

    Raises:
        PostRefusedError: A check fails, the head moved, or GitHub refuses.
    """
    fields, body = validate(sub)
    check_policy(sub, fields, git_dir)
    _, owner, name = repo.parts(sub.repository)
    base = f"https://api.github.com/repos/{owner}/{name}/pulls/{sub.number}"
    if pull_head(api("GET", base, token)) != sub.commit:
        message = "the pull request head moved since the run judged it"
        raise PostRefusedError(message)
    found = existing_review(base, token, sub.stamp)
    if found is not None:
        return found
    payload = {
        "commit_id": sub.commit,
        "body": f"{body}\n---\n{sub.stamp}\n",
        "event": str(fields["event"]),
    }
    data = api("POST", f"{base}/reviews", token, payload)
    created = data.get("id") if isinstance(data, dict) else None
    if not isinstance(created, int):
        message = "GitHub returned no review id"
        raise PostRefusedError(message)
    return created


def stamp(run: Run) -> str:
    """The run's identity and the judged head, as the body's last line."""
    request = run.request
    return (
        f"Run {run.files.name}, {request.harness.value} {request.model}, "
        f"judged head {request.commit}."
    )


def posted(run: Run, prepared: Prepared) -> bool:
    """Post the run's review and record it; False when it was not posted."""
    workspace = Path(prepared.paths.worktree)
    try:
        number, text = find_draft(workspace)
        token = credentials.review_post_token(run.request, run.settings)
        if token is None:
            message = "reviews App token unavailable"
            raise PostRefusedError(message)  # noqa: TRY301
        sub = Submission(
            run.request.repo, number, text, run.request.commit or "", stamp(run)
        )
        identifier = create(sub, token, workspace / ".git")
    except (PostRefusedError, OSError, ValueError) as refusal:
        run.files.write("log", f"review not posted: {refusal}\n")
        return False
    run.files.write("review", f"{run.request.repo}#{number} {identifier}")
    return True


def main(argv: list[str], prog: str) -> int:
    """Post this worktree's review draft; the token is on stdin."""
    parser = argparse.ArgumentParser(prog=prog, description=main.__doc__)
    parser.add_argument("number", type=int)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--stamp", required=True)
    args = parser.parse_args(argv)
    path = review.draft_path(args.number)
    try:
        sub = Submission(
            args.repo, args.number, path.read_text(), args.commit, args.stamp
        )
        identifier = create(sub, sys.stdin.read().strip(), path.parent.parent)
    except (PostRefusedError, OSError, ValueError) as refusal:
        print(f"{prog}: {refusal}", file=sys.stderr)
        return 1
    print(identifier)
    return 0

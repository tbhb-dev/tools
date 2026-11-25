"""Pull request decisions from draft text and command output."""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, cast

from tbhbdev.alpha import identity as agent_identity

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

OPENED_KEY = "Opened-by"
OPENED_LINE = re.compile(rf"{OPENED_KEY}: ")
PR_URL = re.compile(r"https://[^/\s]+/([^/\s]+/[^/\s]+)/pull/(\d+)")
# A repository as ``owner/name``: GitHub's characters for each part, and a name
# that is not "." or "..". The owner can't begin with "-", so gh can't read it
# as a flag.
REPO = r"[A-Za-z0-9][A-Za-z0-9-]*/(?!\.+(?:/|\Z))[A-Za-z0-9_.-]+"
NAME_WITH_OWNER = re.compile(REPO)
PR_PAGE = re.compile(rf"https://github\.com/({REPO})/pull/[0-9]+")
EDIT_DRAFT = "run `tbhbdev alpha pr edit DRAFT --pr {number}` to apply the draft"


def body_of(rendered: Sequence[str]) -> str:
    """The description: everything after the title, trimmed."""
    return "\n".join(rendered[1:]).strip("\n") + "\n"


def identity_key(value: str) -> tuple[str, ...]:
    """Who an LLM trailer value names: harness slug, model, session, subagent."""
    blob = value.split()[1]
    attrs = {
        key: val for key, _, val in (pair.partition("=") for pair in blob.split(";"))
    }
    return (
        attrs["harness"].partition(":")[0],
        attrs.get("model", ""),
        attrs.get("session", ""),
        attrs.get("subagent", ""),
    )


def merge_trailers(
    found: Sequence[tuple[str, str]], posting: str | None
) -> list[tuple[str, str]]:
    """Deduplicate (key, value) trailers by identity; Generated-by wins.

    The posting agent's value is added as Assisted-by when its identity is
    absent. Generated-by lines come first, as the commit-msg check requires.
    """
    roles: dict[tuple[str, ...], tuple[str, str]] = {}
    for key, value in found:
        ident = identity_key(value)
        if ident not in roles or (
            key == agent_identity.KEY_GEN and roles[ident][0] != key
        ):
            roles[ident] = (key, value)
    if posting is not None and identity_key(posting) not in roles:
        roles[identity_key(posting)] = (agent_identity.KEY_ASSIST, posting)
    return sorted(roles.values(), key=lambda pair: pair[0] != agent_identity.KEY_GEN)


def trailer_lines(text: str) -> list[tuple[str, str]]:
    """Match LLM trailer lines in Git's parsed trailer output."""
    found = (agent_identity.LINE.match(line) for line in text.splitlines())
    return [(match[1], match[2]) for match in found if match]


def opened_line(body: str) -> str | None:
    """The ``Opened-by`` line of a PR body's final paragraph, or None."""
    paragraph: list[str] = []
    for line in reversed(body.splitlines()):
        if line.strip():
            paragraph.append(line)
        elif paragraph:
            break
    return next((line for line in paragraph if OPENED_LINE.match(line)), None)


def render_description(
    rendered: Sequence[str],
    found: Sequence[tuple[str, str]],
    posting: str | None,
    opened_by: str | None = None,
) -> str:
    """The final body: the description plus the trailers as its last paragraph.

    ``opened_by``, when given, is the paragraph's first line.

    Raises:
        SystemExit: A trailer, or the posting agent's, fails the commit-trailer
            grammar.
    """
    values = [value for _, value in found] + ([posting] if posting else [])
    errors = [
        f"{value}: {why}"
        for value in values
        if (why := agent_identity.check_value(value))
    ]
    if errors:
        raise SystemExit("invalid LLM trailer: " + "; ".join(errors))
    merged = [f"{key}: {value}" for key, value in merge_trailers(found, posting)]
    trailers = ([opened_by] if opened_by else []) + merged
    body = body_of(rendered)
    if not trailers:
        return body
    return body + "\n" + "\n".join(trailers) + "\n"


def target_of_pr(url: object) -> str:
    """The ``owner/name`` in a pull request's github.com URL, or "".

    The URL must be a string of exactly
    ``https://github.com/<owner>/<name>/pull/<digits>``; anything else, a
    missing URL included, names nothing.
    """
    found = PR_PAGE.fullmatch(url) if isinstance(url, str) else None
    return found[1] if found else ""


def create_flags(meta: Mapping[str, object], base: str) -> list[str]:
    """The gh pr create flags the frontmatter asks for."""
    args = ["--base", base]
    if meta.get("draft"):
        args.append("--draft")
    for flag, key in (
        ("--label", "labels"),
        ("--reviewer", "reviewers"),
        ("--assignee", "assignees"),
    ):
        names = cast("list[str]", meta.get(key, []))  # pragma: no mutate
        args += [item for name in names for item in (flag, name)]
    if "milestone" in meta:
        args += ["--milestone", str(meta["milestone"])]
    return args


def parse_gh_json(returncode: int, stdout: str | bytes) -> object:
    """Decode successful gh output."""
    if returncode != 0:
        return None
    try:
        return json.loads(stdout)
    except ValueError:
        return None


def visibility_of(data: object) -> str:
    """Read the base visibility only when GitHub named a repository."""
    if not isinstance(data, dict):
        return ""
    name = data.get("nameWithOwner")
    if not (isinstance(name, str) and NAME_WITH_OWNER.fullmatch(name)):
        return ""
    return str(data.get("visibility") or "")


def create_refusal(returncode: int, stdout: str, stderr: str) -> str | None:
    """Explain why the existing-PR lookup prevents creation, if it does."""
    data = parse_gh_json(returncode, stdout)
    if isinstance(data, dict) and all(
        type(data.get(key)) is kind
        for key, kind in (("state", str), ("number", int), ("url", str))
    ):
        data = cast("dict[str, object]", data)  # pragma: no mutate
        if data["state"] == "OPEN":
            return (
                f"error: PR {data['number']} ({data['url']}) is already open; "
                + EDIT_DRAFT.format(number=data["number"])
            )
        return None
    if returncode and "no pull requests found" in stderr.lower():
        return None
    output = (stderr or stdout).rstrip("\n")
    return f"cannot check for an open PR: {output}"


def create_error(returncode: int, stdout: str, stderr: str) -> str | None:
    """Combine the failure streams and advise how to edit an existing PR."""
    output = "\n".join(filter(None, (stdout.rstrip("\n"), stderr.rstrip("\n"))))
    already_exists = "already exists" in output.lower()
    if not (returncode or already_exists):
        return None
    if already_exists:
        found = PR_URL.search(output)
        number = found[2] if found else "<n>"
        return f"{output}\ngh pr create failed; " + EDIT_DRAFT.format(number=number)
    return output

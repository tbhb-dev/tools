"""Read and change GitHub pull request review threads."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, TypedDict, cast

from tbhbdev.alpha.review_doc import ReviewError

if TYPE_CHECKING:
    from collections.abc import Iterator

    from tbhbdev.alpha.review_doc import Api

THREADS = """\
query($owner: String!, $repo: String!, $pr: Int!, $after: String) {
  repository(owner: $owner, name: $repo) {
    pullRequest(number: $pr) {
      reviewThreads(first: 100, after: $after) {
        pageInfo { hasNextPage endCursor }
        nodes {
          id path line originalLine isResolved
          comments(first: 1) { nodes { fullDatabaseId author { login } body } }
        }
      }
    }
  }
}
"""
THREAD_TARGET = """\
query($id: ID!) {
  node(id: $id) {
    ... on PullRequestReviewThread {
      pullRequest { number repository { nameWithOwner } }
    }
  }
}
"""
THREAD_ID = re.compile(r"PRRT_[A-Za-z0-9_-]+|[1-9][0-9]*")


class Comment(TypedDict):
    fullDatabaseId: str | None  # noqa: V107 -- GraphQL key read by subscript.
    author: dict[str, str] | None
    body: str


class Thread(TypedDict):
    id: str
    path: str
    line: int | None
    originalLine: int | None  # noqa: V107 -- GraphQL key read by subscript.
    isResolved: bool  # noqa: V107 -- GraphQL key read by subscript.
    comments: dict[str, list[Comment]]


type Object = dict[str, object]
type ThreadPage = dict[str, list[Thread] | Object]
type Repository = dict[str, dict[str, ThreadPage]]


def graphql(api: Api, query: str, **variables: object) -> dict[str, object]:
    response = api.request("POST", "graphql", {"query": query, "variables": variables})
    match cast("dict[str, object]", response):  # pragma: no mutate -- cast
        case {"data": dict(data)}:
            return cast("dict[str, object]", data)  # pragma: no mutate -- cast
        case _:
            message = "GitHub returned no GraphQL data"
            raise ReviewError(message)


def threads(api: Api, repo: str, pr: int) -> Iterator[Thread]:
    owner, name = repo.split("/", 1)  # pragma: no mutate -- origin has one slash
    after = None
    while True:
        data = graphql(api, THREADS, owner=owner, repo=name, pr=pr, after=after)
        match cast("dict[str, Repository]", data):  # pragma: no mutate -- cast
            case {"repository": {"pullRequest": {"reviewThreads": {
                "nodes": list(nodes),
                "pageInfo": {"hasNextPage": bool(more), "endCursor": cursor},
            }}}}:  # fmt: skip
                yield from nodes
                if not more:
                    return
                if isinstance(cursor, str) and cursor != after:
                    after = cursor
                    continue
            case _:
                pass
        message = "GitHub returned no usable review thread page"
        raise ReviewError(message)


def list_rows(api: Api, repo: str, pr: int, *, unresolved: bool) -> Iterator[str]:
    for thread in threads(api, repo, pr):
        resolved = thread["isResolved"]
        if unresolved and resolved:
            continue
        line = thread["line"] or thread["originalLine"] or "-"
        state = "resolved" if resolved else "unresolved"
        comment = next(iter(thread["comments"]["nodes"]), None)
        author, summary = "-", ""
        if comment is not None:
            author = comment["author"]["login"] if comment["author"] else "-"
            summary = next(iter(comment["body"].splitlines()), "")
        yield f"{thread['id']}\t{thread['path']}:{line}\t{state}\t{author}\t{summary}"


def comment_thread(api: Api, repo: str, pr: int, comment_id: str) -> str:
    # GitHub exposes the root comment on a reply, and threads on the PR.
    comment = api.request("GET", f"repos/{repo}/pulls/comments/{comment_id}")
    match cast("dict[str, object]", comment):  # pragma: no mutate -- cast
        case {"id": int(found), **fields} if str(found) == comment_id:
            root = str(fields.get("in_reply_to_id") or found)
        case _:
            message = f"GitHub did not return review comment {comment_id}"
            raise ReviewError(message)
    for thread in threads(api, repo, pr):
        if any(c["fullDatabaseId"] == root for c in thread["comments"]["nodes"]):
            return thread["id"]
    message = f"review comment {comment_id} has no thread on this pull request"
    raise ReviewError(message)


def thread_id(api: Api, repo: str, pr: int, thread: str) -> str:
    if THREAD_ID.fullmatch(thread) is None:
        message = "expected a PRRT_ thread ID or a positive review comment database ID"
        raise ReviewError(message)
    if not thread.startswith("PRRT_"):
        return comment_thread(api, repo, pr, thread)
    data = graphql(api, THREAD_TARGET, id=thread)
    node = data.get("node")
    match cast("dict[str, dict[str, Object | int]] | None", node):  # pragma: no mutate
        case {"pullRequest": {
            "number": number, "repository": {"nameWithOwner": name},
        }} if (number, name) == (pr, repo):  # fmt: skip
            return thread
        case _:
            message = f"thread {thread} is not on this pull request"
            raise ReviewError(message)


def update(api: Api, repo: str, pr: int, thread: str, command: str) -> str:
    """Check a thread's target, change its state, and report GitHub's result."""
    target = thread_id(api, repo, pr, thread)
    operation = f"{command}ReviewThread"
    mutation = (
        "mutation($id: ID!) {"
        f" {operation}(input: {{threadId: $id}}) {{ thread {{ id isResolved }} }}"
        " }"
    )
    result = graphql(api, mutation, id=target)
    payload = result.get(operation)
    match cast("dict[str, Object] | None", payload):  # pragma: no mutate -- cast
        case {"thread": {"id": changed, "isResolved": resolved}} if (
            changed == target and resolved is (command == "resolve")
        ):
            return f"{command}d {target}"
        case _:
            message = f"GitHub did not confirm {command} for thread {target}"
            raise ReviewError(message)

"""Tests for listing and changing pull request review threads."""

import pytest
from test_alpha_review_doc import FakeApi

from tbhbdev.alpha import review, review_threads

REPO = "tbhb-dev/tools"
NUMBER = 7
THREAD = "PRRT_ab-c"
ROOT = 4632604042
REPLY = 4635463433
NODE = {"pullRequest": {"number": NUMBER, "repository": {"nameWithOwner": REPO}}}
WRONG_PR = {"pullRequest": NODE["pullRequest"] | {"number": 8}}
WRONG_REPO = {
    "pullRequest": NODE["pullRequest"] | {"repository": {"nameWithOwner": "o/r"}}
}
BAD_SELECTOR = "expected a PRRT_ thread ID or a positive review comment database ID"
WRONG_TARGET = f"thread {THREAD} is not on this pull request"
OPEN = {
    "id": THREAD,
    "path": "src/tool.py",
    "line": 12,
    "originalLine": 9,
    "isResolved": False,
    "comments": {"nodes": [{
        "fullDatabaseId": str(ROOT), "author": {"login": "agent"},
        "body": "Handle missing input.\r\nHere is how.",
    }]},
}  # fmt: skip
OUTDATED = OPEN | {
    "id": "PRRT_outdated",
    "line": None,
    "isResolved": True,
    "comments": {"nodes": [{"fullDatabaseId": None, "author": None, "body": ""}]},
}
NO_COMMENTS: list[review_threads.Comment] = []
FILE = OPEN | {
    "id": "PRRT_file", "line": None, "originalLine": None,
    "comments": {"nodes": NO_COMMENTS},
}  # fmt: skip


def page(
    *nodes: object, cursor: object = None, more: bool = False
) -> dict[str, object]:
    page_info = {"endCursor": cursor, "hasNextPage": more}
    connection = {"nodes": list(nodes), "pageInfo": page_info}
    return {"data": {"repository": {"pullRequest": {"reviewThreads": connection}}}}


def reply(**data: object) -> dict[str, object]:
    return {"data": data}


def setup_api(monkeypatch: pytest.MonkeyPatch, *responses: object) -> FakeApi:
    api = FakeApi(*responses)
    monkeypatch.setattr(review, "GhApi", lambda: api)
    monkeypatch.setattr(review.pr, "origin_repository", lambda: REPO)
    return api


def graphql_call(query: str, **variables: object) -> tuple[str, str, dict[str, object]]:
    return "POST", "graphql", {"query": query, "variables": variables}


def read_page(after: str | None = None) -> tuple[str, str, dict[str, object]]:
    return graphql_call(
        review_threads.THREADS, owner="tbhb-dev", repo="tools", pr=NUMBER, after=after
    )


def mutate(command: str) -> tuple[str, str, dict[str, object]]:
    query = (
        f"mutation($id: ID!) {{ {command}ReviewThread(input: {{threadId: $id}})"
        " { thread { id isResolved } } }"
    )
    return graphql_call(query, id=THREAD)


@pytest.mark.parametrize("unresolved", [False, True])
def test_threads_cli(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    *,
    unresolved: bool,
) -> None:
    api = setup_api(
        monkeypatch, page(OPEN, cursor="next", more=True), page(OUTDATED, FILE)
    )
    flags = ["--unresolved"] if unresolved else []
    assert review.main(["threads", "7", *flags], "review") == 0
    expected = f"{THREAD}\tsrc/tool.py:12\tunresolved\tagent\tHandle missing input.\n"
    if not unresolved:
        expected += "PRRT_outdated\tsrc/tool.py:9\tresolved\t-\t\n"
    expected += "PRRT_file\tsrc/tool.py:-\tunresolved\t-\t\n"
    assert capsys.readouterr() == (expected, "")
    assert api.calls == [read_page(), read_page("next")]


@pytest.mark.parametrize(
    ("command", "resolved"), [("resolve", True), ("unresolve", False)]
)
def test_set_thread_state(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    command: str,
    *,
    resolved: bool,
) -> None:
    operation = f"{command}ReviewThread"
    changed = reply(**{operation: {"thread": {"id": THREAD, "isResolved": resolved}}})
    api = setup_api(monkeypatch, reply(node=NODE), changed)
    assert review.main([command, "7", THREAD], "review") == 0
    assert capsys.readouterr() == (f"{command}d {THREAD}\n", "")
    assert api.calls == [
        graphql_call(review_threads.THREAD_TARGET, id=THREAD),
        mutate(command),
    ]


@pytest.mark.parametrize(
    ("comment", "pages"),
    [({"id": ROOT}, [page(OPEN)]), ({"id": REPLY, "in_reply_to_id": ROOT},
        [page(OUTDATED, cursor="next", more=True), page(OPEN)]),
    ],
)  # fmt: skip
def test_resolve_by_comment_database_id(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    comment: dict[str, int],
    pages: list[dict[str, object]],
) -> None:
    changed = reply(resolveReviewThread={"thread": {"id": THREAD, "isResolved": True}})
    api = setup_api(monkeypatch, comment, *pages, changed)
    comment_id = comment["id"]
    assert review.main(["resolve", "7", str(comment_id)], "review") == 0
    assert capsys.readouterr() == (f"resolved {THREAD}\n", "")
    assert api.calls[0] == ("GET", f"repos/{REPO}/pulls/comments/{comment_id}", None)
    assert api.calls[1:-1] == [read_page(), read_page("next")][: len(pages)]
    assert api.calls[-1] == mutate("resolve")


@pytest.mark.parametrize(
    ("selector", "responses", "message"),
    [
        ("0", [], BAD_SELECTOR),
        (THREAD, [{}], "GitHub returned no GraphQL data"),
        (THREAD, [reply(node=None)], WRONG_TARGET),
        (THREAD, [reply(node=WRONG_PR)], WRONG_TARGET),
        (THREAD, [reply(node=WRONG_REPO)], WRONG_TARGET),
        (str(ROOT), [{"id": REPLY}], f"GitHub did not return review comment {ROOT}"),
        (str(ROOT), [{"id": ROOT}, page(OUTDATED)],
         f"review comment {ROOT} has no thread on this pull request"),
    ],
)  # fmt: skip
def test_refusals_stop_before_mutation(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    selector: str,
    responses: list[object],
    message: str,
) -> None:
    api = setup_api(monkeypatch, *responses)
    assert review.main(["resolve", "7", selector], "review") == 1
    assert capsys.readouterr() == ("", f"PR 7 resolve: {message}\n")
    assert len(api.calls) == len(responses)


@pytest.mark.parametrize(
    "bad_pages", [[reply(repository=None)], [page(more=True)],
        [page(cursor="next", more=True), page(cursor="next", more=True)]],
)  # fmt: skip
def test_invalid_thread_pages(bad_pages: list[object]) -> None:
    api = FakeApi(*bad_pages)
    with pytest.raises(
        review_threads.ReviewError,
        match=r"^GitHub returned no usable review thread page$",
    ):
        list(review_threads.list_rows(api, REPO, NUMBER, unresolved=False))


@pytest.mark.parametrize(
    "changed", [None, {"id": THREAD, "isResolved": False},
        {"id": "PRRT_other", "isResolved": True}, {"id": THREAD, "isResolved": 1}],
)  # fmt: skip
def test_mutation_must_confirm_the_requested_state(changed: object) -> None:
    api = FakeApi(reply(node=NODE), reply(resolveReviewThread={"thread": changed}))
    with pytest.raises(
        review_threads.ReviewError, match=f"did not confirm resolve for thread {THREAD}"
    ):
        review_threads.update(api, REPO, NUMBER, THREAD, "resolve")

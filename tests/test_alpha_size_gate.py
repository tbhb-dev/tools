"""Size counter and review reads use fakes; no test runs Bazel."""

import argparse
import json
import subprocess
from unittest.mock import Mock, call

import pytest

from tbhbdev.alpha import pull_request as pr
from tbhbdev.alpha import size_gate as sg

GOOD = {
    "code_lines": 400,
    "code_limit": 400,
    "raw_lines": 1500,
    "raw_limit": 1500,
    "failed": None,
}


@pytest.mark.parametrize("failed", [None, "code", "raw"])
def test_counts(monkeypatch: pytest.MonkeyPatch, failed: str | None) -> None:
    runner = Mock(
        return_value=subprocess.CompletedProcess(
            [], 0, json.dumps({**GOOD, "failed": failed}), ""
        )
    )
    monkeypatch.setattr(sg.subprocess, "run", runner)
    assert sg.size_counts("origin/main", "HEAD") == sg.SizeCounts(
        400, 400, 1500, 1500, failed
    )
    assert runner.call_count == 1
    assert runner.call_args.args == (["bazelisk", *sg.COUNTER, "origin/main", "HEAD"],)


@pytest.mark.parametrize("error", [OSError("no bazelisk"), None])
@pytest.mark.parametrize("stderr", ["missing target\n", ""])
def test_counter_errors(
    monkeypatch: pytest.MonkeyPatch, error: OSError | None, stderr: str
) -> None:
    runner = Mock(
        side_effect=error, return_value=subprocess.CompletedProcess([], 2, "", stderr)
    )
    monkeypatch.setattr(sg.subprocess, "run", runner)
    reason = "no bazelisk" if error else (stderr.strip() or "counter exited 2")
    with pytest.raises(sg.SizeCountError, match=reason):
        sg.size_counts("base", "head")


@pytest.mark.parametrize("stdout", ["", "{}\n{}", "not JSON"])
def test_bad_json(monkeypatch: pytest.MonkeyPatch, stdout: str) -> None:
    monkeypatch.setattr(
        sg.subprocess,
        "run",
        Mock(return_value=subprocess.CompletedProcess([], 0, stdout, "")),
    )
    with pytest.raises(sg.SizeCountError, match="invalid JSON"):
        sg.size_counts("base", "head")


@pytest.mark.parametrize(
    "data",
    [
        None,
        [],
        {},
        *[
            {key: value for key, value in GOOD.items() if key != missing}
            for missing in GOOD
        ],
        *[
            {**GOOD, key: value}
            for key in sg.COUNT_KEYS
            for value in (True, None, "1", 1.0, [0])
        ],
        *[{**GOOD, "failed": value} for value in (True, 0, [0], {"x": 0}, "other")],
    ],
)
def test_bad_counts(monkeypatch: pytest.MonkeyPatch, data: object) -> None:
    monkeypatch.setattr(
        sg.subprocess,
        "run",
        Mock(return_value=subprocess.CompletedProcess([], 0, json.dumps(data), "")),
    )
    with pytest.raises(sg.SizeCountError, match="invalid size counts"):
        sg.size_counts("base", "head")


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        (None, False),
        ({}, False),
        (
            {
                "sha": "other",
                "statuses": [{"context": "size-review", "state": "success"}],
            },
            False,
        ),
        ({"sha": "head"}, False),
        ({"sha": "head", "statuses": {}}, False),
        ({"sha": "head", "statuses": []}, False),
        *[
            ({"sha": "head", "statuses": [status]}, False)
            for status in (
                None,
                {"state": "success"},
                {"context": "other", "state": "success"},
                {"context": "size-review", "state": "pending"},
                {"context": "size-review", "state": "failure"},
            )
        ],
        (
            {
                "sha": "head",
                "statuses": [{"context": "size-review", "state": "success"}],
            },
            True,
        ),
    ],
)
def test_review(monkeypatch: pytest.MonkeyPatch, data: object, expected: bool) -> None:
    reader = Mock(return_value=data)
    monkeypatch.setattr(sg, "gh_api", reader)
    assert sg.has_size_review_success("owner/repo", "head") is expected
    reader.assert_called_once_with("GET", "repos/owner/repo/commits/head/status")


@pytest.mark.parametrize(
    ("counts", "problem"),
    [
        (sg.SizeCounts(400, 400, 1500, 1500, None), ""),
        (sg.SizeCounts(401, 400, 1400, 1500, "code"), "401 lines, limit 400"),
        (sg.SizeCounts(401, 400, 1501, 1500, "code"), "401 lines, limit 400"),
        (sg.SizeCounts(100, 400, 1501, 1500, "raw"), "1501 lines, limit 1500"),
    ],
)
@pytest.mark.usefixtures("pr_tools")
def test_pr_check(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    counts: sg.SizeCounts,
    problem: str,
) -> None:
    checked = pr.Checked([])
    lint = Mock(return_value=checked)
    monkeypatch.setattr(pr, "read_checked", lint)
    counter = Mock(return_value=counts)
    monkeypatch.setattr(sg, "size_counts", counter)
    monkeypatch.setattr(pr, "git", Mock(return_value=(0, "")))
    monkeypatch.setattr(pr, "base_of", Mock(return_value="main"))
    assert pr.command(
        argparse.Namespace(command="check", draft="draft.md"), {}
    ) == bool(problem)
    lint.assert_called_once_with("draft.md")
    counter.assert_called_once_with(
        "refs/remotes/origin/main", "HEAD", bazelisk="/tools/bazelisk"
    )
    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured.out.startswith(
        f"{counts.code_lines} code lines, limit 400\n"
        f"{counts.raw_lines} raw lines, limit 1500\n"
    )
    if problem:
        assert checked.problems == [(1, f"{problem}: split into stacked PRs")]
        assert captured.out.endswith(f"draft.md:1: {problem}: split into stacked PRs\n")
    else:
        assert captured.out.endswith("OK: the draft conforms\n")


@pytest.mark.usefixtures("pr_tools")
def test_pr_check_unavailable(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(pr, "git", Mock(return_value=(0, "")))
    monkeypatch.setattr(pr, "base_of", Mock(return_value="main"))
    monkeypatch.setattr(pr, "read_checked", Mock(return_value=pr.Checked([])))
    monkeypatch.setattr(
        sg, "size_counts", Mock(side_effect=sg.SizeCountError("missing target"))
    )
    assert pr.command(argparse.Namespace(command="check", draft="draft.md"), {}) == 0
    captured = capsys.readouterr()
    assert captured.err == "size count unavailable: missing target\n"
    assert captured.out == "OK: the draft conforms\n"


REAL_TARGET_IS_ORIGIN = pr.target_is_origin


@pytest.fixture
def pr_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    def paths(names: tuple[str, ...], **_options: object) -> dict[str, str]:
        return {name: f"/tools/{name}" for name in names}

    monkeypatch.setattr(pr, "find_tools", Mock(side_effect=paths))


@pytest.fixture(autouse=True)
def gh_targets_origin(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pr, "target_is_origin", Mock(return_value=True))


FETCH = ["fetch", "--quiet", "--no-tags", "--no-write-fetch-head", "--refmap="]


def git_exits(code: int = 0) -> Mock:
    """A git mock that exits ``code`` with no output."""
    return Mock(return_value=(code, ""))


@pytest.mark.parametrize("failure", ["count", "over", "base"])
@pytest.mark.usefixtures("pr_tools")
def test_pr_create_size(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    failure: str,
) -> None:
    counts = sg.SizeCounts(401, 400, 500, 1500, "code")
    counter = Mock(return_value=counts)
    if failure == "count":
        counter = Mock(side_effect=sg.SizeCountError("missing target"))
    monkeypatch.setattr(sg, "size_counts", counter)
    git = git_exits(int(failure == "base"))
    monkeypatch.setattr(pr, "git", git)
    monkeypatch.setattr(pr, "base_of", Mock(return_value="main"))
    monkeypatch.setattr(pr, "read_checked", Mock(return_value=pr.Checked([])))
    monkeypatch.setattr(pr, "branch_trailers", Mock(return_value=[]))
    monkeypatch.setattr(pr, "post_text", Mock())
    monkeypatch.setattr(pr, "base_visibility", Mock(return_value="public"))
    post = Mock(return_value=0)
    monkeypatch.setattr(pr, "create_pr", post)
    assert pr.command(
        argparse.Namespace(command="create", draft="d.md", dry_run=False), {}
    ) == (failure == "over")
    captured = capsys.readouterr()
    assert post.called is (failure != "over")
    if failure == "over":
        assert captured.out == "d.md:1: 401 lines, limit 400: split into stacked PRs\n"
    else:
        assert captured.err.startswith("size count unavailable: ")
    fetched = [*FETCH, "origin", "+refs/heads/main:refs/remotes/origin/main"]
    assert call(*fetched) in git.call_args_list
    if failure != "base":
        counter.assert_called_once_with(
            "refs/remotes/origin/main", "HEAD", bazelisk="/tools/bazelisk"
        )


def test_the_base_is_refreshed_without_tags_then_counted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    counter = Mock(return_value=sg.SizeCounts(5, 400, 9, 1500, None))
    monkeypatch.setattr(sg, "size_counts", counter)
    git = git_exits()
    monkeypatch.setattr(pr, "git", git)
    monkeypatch.setattr(pr, "base_of", Mock(return_value="topic"))
    checked = pr.Checked([])
    pr.check_size(argparse.Namespace(command="create", draft="d.md"), checked)
    assert checked.problems == []
    assert git.call_args_list[0] == call(
        *FETCH, "origin", "+refs/heads/topic:refs/remotes/origin/topic"
    )
    counter.assert_called_once_with(
        "refs/remotes/origin/topic", "HEAD", bazelisk="bazelisk"
    )


def test_a_failed_fetch_warns_and_never_counts_a_stale_ref(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    counter = Mock(return_value=sg.SizeCounts(411, 400, 500, 1500, "code"))
    monkeypatch.setattr(sg, "size_counts", counter)

    git = git_exits(1)
    monkeypatch.setattr(pr, "git", git)
    monkeypatch.setattr(pr, "base_of", Mock(return_value="main"))
    checked = pr.Checked([])
    pr.check_size(argparse.Namespace(command="create", draft="d.md"), checked)
    assert checked.problems == []
    assert capsys.readouterr().err == "size count unavailable: cannot fetch main\n"
    counter.assert_not_called()
    assert [c.args[0] for c in git.call_args_list] == ["fetch"]
    assert "--refmap=" in git.call_args_list[0].args


def test_create_warns_and_never_counts_when_gh_targets_another_repository(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    counter = Mock(return_value=sg.SizeCounts(411, 400, 500, 1500, "code"))
    monkeypatch.setattr(sg, "size_counts", counter)
    git = Mock(return_value=(0, ""))
    monkeypatch.setattr(pr, "git", git)
    monkeypatch.setattr(pr, "target_is_origin", Mock(return_value=False))
    monkeypatch.setattr(pr, "base_of", Mock(return_value="main"))
    checked = pr.Checked([])
    pr.check_size(argparse.Namespace(command="create", draft="d.md"), checked)
    assert checked.problems == []
    assert capsys.readouterr().err == (
        "size count unavailable: gh may target a repository other than origin\n"
    )
    counter.assert_not_called()
    git.assert_not_called()


@pytest.mark.parametrize(
    ("view", "origin", "expected"),
    [
        ({"nameWithOwner": "o/r"}, "O/R", True),
        ({"nameWithOwner": "parent/r"}, "o/r", False),
        ({}, "o/r", False),
        (None, "o/r", False),
        ({"nameWithOwner": "o/r"}, SystemExit("cannot read origin"), False),
    ],
)
def test_target_is_origin(
    monkeypatch: pytest.MonkeyPatch,
    view: object,
    origin: str | BaseException,
    expected: bool,
) -> None:
    monkeypatch.setattr(pr, "find_tool", Mock(return_value="gh"))
    monkeypatch.setattr(pr, "run", Mock())
    monkeypatch.setattr(pr, "gh_data", Mock(return_value=view))
    monkeypatch.setattr(pr, "origin_repository", Mock(side_effect=[origin]))
    monkeypatch.delenv("GH_REPO", raising=False)
    assert REAL_TARGET_IS_ORIGIN() is expected


def test_gh_repo_env_means_the_target_is_not_origin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = Mock()
    monkeypatch.setattr(pr, "run", run)
    monkeypatch.setenv("GH_REPO", "parent/r")
    assert REAL_TARGET_IS_ORIGIN() is False
    run.assert_not_called()


def test_a_dry_run_fetches_nothing_and_warns_on_an_excess(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    counts = sg.SizeCounts(401, 400, 500, 1500, "code")
    monkeypatch.setattr(sg, "size_counts", Mock(return_value=counts))
    git = git_exits()
    monkeypatch.setattr(pr, "git", git)
    monkeypatch.setattr(pr, "base_of", Mock(return_value="main"))
    checked = pr.Checked([])
    args = argparse.Namespace(command="create", draft="d.md", dry_run=True)
    pr.check_size(args, checked)
    captured = capsys.readouterr()
    assert checked.problems == []
    assert captured.out == ""
    assert (
        captured.err
        == "warning: d.md:1: 401 lines, limit 400: split into stacked PRs\n"
    )
    assert all(c.args[0] != "fetch" for c in git.call_args_list)
    failing = Mock(return_value=(1, ""))
    monkeypatch.setattr(pr, "git", failing)
    pr.check_size(args, checked)
    assert all(c.args[0] != "fetch" for c in failing.call_args_list)

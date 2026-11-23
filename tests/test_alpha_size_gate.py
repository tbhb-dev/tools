"""Size counter and review reads use fakes; no test runs Bazel."""

import argparse
import json
import subprocess
from unittest.mock import Mock

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
    assert runner.call_args.args == ([*sg.COUNTER, "origin/main", "HEAD"],)


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
    monkeypatch.setattr(pr, "git", Mock(return_value=(0, "refs/remotes/origin/main")))
    assert pr.command(
        argparse.Namespace(command="check", draft="draft.md"), {}
    ) == bool(problem)
    lint.assert_called_once_with("draft.md")
    counter.assert_called_once_with("refs/remotes/origin/main", "HEAD")
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


def test_counter_targets_the_rules_repository() -> None:
    assert sg.COUNTER[2] == "@tbhb_rules//lint:pr_size_count"


def test_pr_check_unavailable(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(pr, "git", Mock(return_value=(0, "refs/remotes/origin/main")))
    monkeypatch.setattr(pr, "read_checked", Mock(return_value=pr.Checked([])))
    monkeypatch.setattr(
        sg, "size_counts", Mock(side_effect=sg.SizeCountError("missing target"))
    )
    assert pr.command(argparse.Namespace(command="check", draft="draft.md"), {}) == 0
    captured = capsys.readouterr()
    assert (
        captured.err
        == "size count unavailable (@tbhb_rules//lint:pr_size_count): missing target\n"
    )
    assert captured.out == "OK: the draft conforms\n"

"""Open a run's pull request from either draft source."""

from __future__ import annotations

import os
import subprocess
from io import BufferedReader, FileIO
from typing import TYPE_CHECKING, override

import httpx
import pytest

from tbhbdev import gitleaks_check
from tbhbdev.alpha.agent import host_tools, open_pr
from tbhbdev.alpha.agent.model import Invalid

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

DRAFT = """---
draft: false
base: main
---

# feat(example): add a thing

## Why

Because.

## Behavior change

A thing.

## Decisions

None.

## Needs human judgment

None.

## Risk

None.

## Verification

Ran it.
"""


POLICY = "[extend]\nuseDefault = true\n"
WARNING = ("log", "draft committed: it will merge into main; remove it\n")


def write_draft(directory: Path, text: str) -> Path:
    path = directory / open_pr.DRAFT
    path.parent.mkdir(exist_ok=True)
    path.write_text(text)
    return path


def commit_draft(
    directory: Path,
    text: str | None,
    *,
    initial: bool = False,
    policy: bool = True,
) -> open_pr.PublishedRange:
    def git(*args: str) -> str:
        return subprocess.check_output(
            ["git", *args],
            cwd=directory,
            text=True,
        ).strip()

    def commit() -> str:
        git("add", ".")
        git("commit", "--allow-empty", "-qm", "x")
        return git("rev-parse", "HEAD")

    git("init", "-q")
    git("config", "user.name", "t")
    git("config", "user.email", "t@invalid")
    git("config", "commit.gpgsign", "false")
    if policy:
        (directory / ".gitleaks.toml").write_text(POLICY)
    if initial:
        write_draft(directory, DRAFT)
    before = commit()
    if text is not None:
        write_draft(directory, text)
        (directory / ".gitleaks.toml").write_text("untrusted")
    head = commit()
    (directory / open_pr.DRAFT).unlink(missing_ok=True)
    return open_pr.PublishedRange(directory, directory / ".git", before, head)


def accept_warning(suffix: str, text: str) -> None:
    assert (suffix, text) == WARNING


def open_draft(
    published: open_pr.PublishedRange,
    write: Callable[[str, str], None] = accept_warning,
) -> int:
    return open_pr.open_pull_request(
        published, "tbhb-dev/tools", "agent/r1", "tok", write
    )


@pytest.fixture(autouse=True)
def scans(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    original = host_tools.resolve
    seen: list[str] = []

    def resolve(name: str) -> str:
        return "fixture-scanner" if name == "gitleaks" else original(name)

    def scan(
        text: str, what: str, *, root: Path, gitleaks: str, visibility: str
    ) -> None:
        assert what == "PR draft"
        assert root.name == ".git"
        assert (root / ".gitleaks.toml").read_text() == POLICY
        assert gitleaks == "fixture-scanner"
        assert visibility == "PRIVATE"
        seen.append(text)

    def get(url: str, **options: object) -> httpx.Response:
        assert url == "https://api.github.com/repos/tbhb-dev/tools"
        assert "Bearer tok" in str(options["headers"])
        return reply(200, {"full_name": "TBHB-dev/Tools", "visibility": "private"})

    monkeypatch.setattr(host_tools, "resolve", resolve)
    monkeypatch.setattr(gitleaks_check, "scan_text", scan)
    monkeypatch.setattr(httpx, "get", get)
    return seen


def reply(status: int, data: object) -> httpx.Response:
    return httpx.Response(status, json=data, request=httpx.Request("POST", "https://x"))


def test_opens_the_pr_from_the_pushed_draft(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scans: list[str]
) -> None:
    published = commit_draft(tmp_path, DRAFT)
    seen: dict[str, object] = {}

    def post(url: str, **options: object) -> httpx.Response:
        seen["url"] = url
        seen.update(options)
        return reply(201, {"number": 12})

    monkeypatch.setattr(httpx, "post", post)
    warnings: list[tuple[str, str]] = []

    def warn(suffix: str, text: str) -> None:
        warnings.append((suffix, text))

    number = open_draft(published, warn)
    assert number == 12
    assert warnings == [WARNING]
    assert scans == [DRAFT]
    assert seen["url"] == "https://api.github.com/repos/tbhb-dev/tools/pulls"
    payload = seen["json"]
    assert isinstance(payload, dict)
    assert payload["title"] == "feat(example): add a thing"
    assert payload["head"] == "agent/r1"
    assert payload["base"] == "main"
    assert payload["draft"] is False
    assert "## Why" in payload["body"]
    headers = seen["headers"]
    assert isinstance(headers, dict)
    assert headers["Authorization"] == "Bearer tok"
    assert headers["Accept"] == "application/vnd.github+json"
    assert seen["timeout"] is open_pr.TIMEOUT


def test_publication_git_isolated_from_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    published = commit_draft(tmp_path, DRAFT)
    flags = "--no-ext-diff --no-textconv --no-renames --diff-filter=A --name-only"
    output = iter([open_pr.DRAFT.encode(), POLICY.encode()])
    expected = {
        "HOME": str(published.git_dir),
        "PATH": host_tools.search(),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
    }

    def git(args: list[str], **options: object) -> subprocess.CompletedProcess[bytes]:
        if args[3] == "diff":
            assert args[4:-4] == flags.split()
            assert args[-1] == open_pr.DRAFT
        assert options["cwd"] == published.git_dir
        assert options["env"] == expected
        return subprocess.CompletedProcess(args, 0, next(output))

    monkeypatch.setattr(subprocess, "run", git)
    assert open_pr.check_range(published, accept_warning) == POLICY.encode()


def test_a_missing_draft_is_refused(tmp_path: Path) -> None:
    published = commit_draft(tmp_path, None)
    with pytest.raises(
        open_pr.OpenRefusedError, match=r"no \.github/PULL_REQUEST_DRAFT"
    ):
        open_draft(published)


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("---\nbase: [\n---\n\n# t\n", "frontmatter is invalid"),
        ("---\n- base: release/1.x\n---\n\n# t\n", "frontmatter is invalid"),
        ("---\nrelease\n---\n\n# t\n", "frontmatter is invalid"),
        ("---\n# t\n", "frontmatter is invalid"),
        ("---\nbase: 3\n---\n\n# t\n", "invalid base branch"),
        ("---\nbase: ''\n---\n\n# t\n", "invalid base branch"),
        ("---\nbase: main\n---\n\nplain text\n", "no H1 title"),
        ("---\nbase: main\n---\n\n# \n", "no H1 title"),
    ],
)
def test_a_malformed_draft_is_refused(tmp_path: Path, text: str, message: str) -> None:
    published = commit_draft(tmp_path, text)
    with pytest.raises(open_pr.OpenRefusedError, match=message) as captured:
        open_draft(published)
    assert captured.value.__context__ is None


def never_asked() -> str:
    raise AssertionError


@pytest.mark.parametrize(
    "text", ["# t\n\nbody\n", "---\ndraft: false\n---\n\n# t\n\nbody\n"]
)
def test_a_draft_without_a_base_takes_the_repository_default_branch(
    text: str,
) -> None:
    assert open_pr.parse_draft(text, lambda: "trunk").base == "trunk"


def test_a_draft_with_a_base_never_asks_for_the_default_branch() -> None:
    assert (
        open_pr.parse_draft("---\nbase: dev\n---\n\n# t\n", never_asked).base == "dev"
    )


def api_reply(status: int, data: object) -> httpx.Response:
    return httpx.Response(status, json=data, request=httpx.Request("GET", "https://x"))


def test_the_default_branch_comes_from_one_api_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, object] = {}

    def get(url: str, **options: object) -> httpx.Response:
        seen["url"] = url
        seen["headers"] = options["headers"]
        return api_reply(200, {"default_branch": "trunk"})

    monkeypatch.setattr(httpx, "get", get)
    assert open_pr.repository_default_branch("tbhb-dev/tools", "tok") == "trunk"
    assert seen["url"] == "https://api.github.com/repos/tbhb-dev/tools"
    assert isinstance(seen["headers"], dict)
    assert seen["headers"]["Authorization"] == "Bearer tok"


@pytest.mark.parametrize(
    "answer",
    [
        api_reply(404, {}),
        api_reply(200, ["main"]),
        api_reply(200, {}),
        api_reply(200, {"default_branch": ""}),
        api_reply(200, {"default_branch": 3}),
        httpx.ConnectError("down"),
        ValueError("not json"),
    ],
)
def test_an_unreadable_default_branch_refuses(
    monkeypatch: pytest.MonkeyPatch, answer: httpx.Response | Exception
) -> None:
    def get(_url: str, **_options: object) -> httpx.Response:
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(httpx, "get", get)
    with pytest.raises(open_pr.OpenRefusedError, match="default branch is unreadable"):
        open_pr.repository_default_branch("tbhb-dev/tools", "tok")


def test_a_draft_without_a_base_opens_against_the_api_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    published = commit_draft(tmp_path, "# t\n\nbody\n")
    seen: dict[str, object] = {}

    def get(_url: str, **_options: object) -> httpx.Response:
        data = {
            "default_branch": "trunk",
            "full_name": "tbhb-dev/tools",
            "visibility": "private",
        }
        return api_reply(200, data)

    def post(_url: str, **options: object) -> httpx.Response:
        seen.update(options)
        return reply(201, {"number": 4})

    monkeypatch.setattr(httpx, "get", get)
    monkeypatch.setattr(httpx, "post", post)
    assert open_draft(published) == 4
    assert isinstance(seen["json"], dict)
    assert seen["json"]["base"] == "trunk"


def test_the_frontmatter_sets_the_base_and_draft_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    text = "---\nbase: release\ndraft: true\n---\n\n# fix(x): y\n\n  ## Why\n\nZ.X\n"
    published = commit_draft(tmp_path, text)
    seen: dict[str, object] = {}

    def post(_url: str, **options: object) -> httpx.Response:
        seen.update(options)
        return reply(201, {"number": 3})

    monkeypatch.setattr(httpx, "post", post)
    open_draft(published)
    assert seen["json"] == {
        "title": "fix(x): y",
        "body": "  ## Why\n\nZ.X\n",
        "head": "agent/r1",
        "base": "release",
        "draft": True,
    }


@pytest.mark.parametrize(
    ("response", "message"),
    [
        (httpx.Response(422, text="private response detail"), "status 422"),
        (reply(401, {"message": "private response detail"}), "token with status 401"),
        (reply(201, {"id": 1}), "status 201"),
        (reply(201, []), "status 201"),
    ],
)
def test_a_refusing_api_is_reported_without_its_text(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    response: httpx.Response,
    message: str,
) -> None:
    published = commit_draft(tmp_path, DRAFT)

    def post(*_args: object, **_options: object) -> httpx.Response:
        return response

    monkeypatch.setattr(httpx, "post", post)
    with pytest.raises(open_pr.OpenRefusedError, match=message) as captured:
        open_draft(published)
    assert "private response detail" not in str(captured.value)


@pytest.mark.parametrize("failure", [httpx.ConnectError("down"), ValueError("x")])
def test_a_transport_failure_is_reported_by_type(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: Exception
) -> None:
    published = commit_draft(tmp_path, DRAFT)

    def post(*_args: object, **_options: object) -> httpx.Response:
        raise failure

    monkeypatch.setattr(httpx, "post", post)
    with pytest.raises(open_pr.OpenRefusedError, match=type(failure).__name__):
        open_draft(published)


@pytest.mark.parametrize(
    ("ending", "tracked"), [("\n", False), ("\r\n", True), ("\r", True)]
)
def test_worktree_draft_takes_priority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    scans: list[str],
    ending: str,
    *,
    tracked: bool,
) -> None:
    published = commit_draft(
        tmp_path, "bad committed draft" if tracked else None, initial=tracked
    )
    (published.git_dir / "info" / "exclude").write_text(open_pr.DRAFT + "\n")
    worktree_text = DRAFT.replace("# feat", "# fix").replace("\n", ending)
    write_draft(tmp_path, worktree_text)

    def no_warning(suffix: str, text: str) -> None:
        pytest.fail(f"unexpected {suffix}: {text}")

    def post(_url: str, **options: object) -> httpx.Response:
        assert scans == [worktree_text]
        payload = options["json"]
        assert isinstance(payload, dict)
        assert payload["title"] == "fix(example): add a thing"
        assert payload["body"] == open_pr.parse_draft(DRAFT, never_asked).body
        return reply(201, {"number": 12})

    monkeypatch.setattr(httpx, "post", post)
    assert open_draft(published, no_warning) == 12


@pytest.mark.parametrize(
    "kind",
    ["file-link", "folder-link", "root-link", "fifo", "uid", "size", "encoding"],
)
def test_unsafe_worktree_draft_refuses_without_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str, scans: list[str]
) -> None:
    workspace = tmp_path / "run"
    workspace.mkdir()
    published = commit_draft(workspace, DRAFT)
    path = workspace / open_pr.DRAFT
    outside = write_draft(tmp_path, DRAFT)
    message = "cannot read the worktree PR draft safely"

    match kind:
        case "file-link":
            path.symlink_to(outside)
        case "folder-link":
            path.parent.rmdir()
            path.parent.symlink_to(outside.parent, target_is_directory=True)
        case "root-link":
            link = tmp_path / "link"
            link.symlink_to(workspace, target_is_directory=True)
            published = published._replace(workspace=link)
        case "fifo":
            os.mkfifo(path)
            message = "the worktree PR draft must be an owned regular file"
        case "uid":
            path.write_text(DRAFT)

            def uid() -> int:
                return path.stat().st_uid + 1

            monkeypatch.setattr(os, "getuid", uid)
            message = "the worktree PR draft must be an owned regular file"
        case "size":
            path.write_bytes(b"x" * (64 * 1024 + 1))
            message = "the worktree PR draft exceeds 64 KiB"
        case _:
            path.write_bytes(b"\xff")
            message = "the PR draft must use UTF-8"

    with pytest.raises(open_pr.OpenRefusedError, match=rf"^{message}$"):
        open_draft(published)
    assert scans == []


def test_worktree_draft_size_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    text = DRAFT.ljust(64 * 1024)
    write_draft(tmp_path, text)
    original = os.open

    def open_at(path: str | Path, flags: int, *, dir_fd: int | None = None) -> int:
        required = os.O_NONBLOCK if path == open_pr.DRAFT_NAME else os.O_DIRECTORY
        assert flags == os.O_RDONLY | os.O_NOFOLLOW | required
        return original(path, flags, dir_fd=dir_fd)

    class DraftReader(BufferedReader):
        @override
        def read(self, size: int | None = -1, /) -> bytes:
            assert size == 64 * 1024 + 1
            return super().read(size)

    def fdopen(fd: int, mode: str) -> BufferedReader:
        return DraftReader(FileIO(fd, mode))

    monkeypatch.setattr(os, "open", open_at)
    monkeypatch.setattr(os, "fdopen", fdopen)
    assert open_pr.read_worktree_draft(tmp_path) == text.encode()


@pytest.mark.parametrize("reason", ["no-base", "bad-range", "no-policy"])
def test_unavailable_scan_policy_refuses(
    tmp_path: Path, reason: str, scans: list[str]
) -> None:
    published = commit_draft(tmp_path, None, policy=False)
    write_draft(tmp_path, DRAFT)
    if reason == "no-base":
        published = published._replace(before=None)
        message = r"^cannot check the PR draft without a pre-run commit$"
    elif reason == "bad-range":
        published = published._replace(before="0" * 40)
        message = r"^cannot check whether the pushed range adds the PR draft$"
    else:
        message = "cannot scan the PR draft without"
    with pytest.raises(open_pr.OpenRefusedError, match=message):
        open_draft(published)
    assert scans == []


UNNAMED = "the scanner refused for a reason the log doesn't name"
SCANNER_MISSING = "the gitleaks executable cannot run"


@pytest.mark.parametrize("committed", [False, True])
@pytest.mark.parametrize(
    ("fields", "reason"),
    [
        ({"kind": "exit"}, UNNAMED),
        ({"kind": "other"}, UNNAMED),
        (
            {"kind": "finding", "lines": (3, 9), "locations": "rule on line 123"},
            "gitleaks finds a secret on lines: 3, 9",
        ),
        ({"kind": "exit", "status": 2}, "gitleaks failed (exit 2)"),
        ({"kind": "signal", "status": -9}, "gitleaks was stopped by a signal"),
        (
            {"kind": "config"},
            "the private gitleaks config or the extends file is unusable",
        ),
        ({"kind": "scanner_missing"}, SCANNER_MISSING),
    ],
)
def test_a_scan_refusal_prevents_posting(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fields: dict[str, object],
    reason: str,
    *,
    committed: bool,
) -> None:
    published = commit_draft(tmp_path, DRAFT if committed else None)
    if not committed:
        write_draft(tmp_path, DRAFT)

    def refuse(text: str, *_args: object, **_kwargs: object) -> None:
        assert text == DRAFT
        message = "SECRET-TEXT fixture-rule"
        raise gitleaks_check.ScanRefused(message, **fields)  # pyrefly: ignore[bad-argument-type]

    def post(*_args: object, **_kwargs: object) -> httpx.Response:
        pytest.fail("unexpected API call")

    monkeypatch.setattr(gitleaks_check, "scan_text", refuse)
    monkeypatch.setattr(httpx, "post", post)
    with pytest.raises(open_pr.OpenRefusedError) as captured:
        open_draft(published)
    assert str(captured.value) == f"PR draft secret scan refused: {reason}"
    assert "SECRET-TEXT" not in str(captured.value)
    assert "123" not in str(captured.value)
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None


def test_a_missing_scanner_executable_is_labeled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    published = commit_draft(tmp_path, DRAFT)

    def missing(*_args: object, **_kwargs: object) -> None:
        message = "host executable gitleaks was not found"
        raise Invalid(message)

    monkeypatch.setattr(gitleaks_check, "scan_text", missing)
    with pytest.raises(open_pr.OpenRefusedError) as captured:
        open_draft(published)
    assert str(captured.value) == f"PR draft secret scan refused: {SCANNER_MISSING}"
    assert captured.value.__context__ is None


@pytest.mark.parametrize("error", [UnicodeError("x")])
def test_a_draft_read_failure_names_its_class(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    published = commit_draft(tmp_path, DRAFT)

    def refuse(*_args: object, **_kwargs: object) -> None:
        raise error

    monkeypatch.setattr(gitleaks_check, "scan_text", refuse)
    with pytest.raises(open_pr.OpenRefusedError) as captured:
        open_draft(published)
    reason = f"{type(error).__name__} while scanning the draft"
    assert str(captured.value) == f"PR draft secret scan refused: {reason}"


NAMED = "tbhb-dev/tools"
UNREADABLE = "visibility lookup failed: unreachable or unreadable\n"
NO_ANSWER = "visibility lookup failed: no usable answer\n"
OTHER = "visibility lookup failed: answer names another repository\n"


def answer_for(visibility: object, name: object = "TBHB-dev/Tools") -> httpx.Response:
    return reply(200, {"full_name": name, "visibility": visibility})


@pytest.mark.parametrize(
    ("answer", "expected", "logged"),
    [
        (answer_for("private"), "PRIVATE", ""),
        (answer_for("public"), "PUBLIC", ""),
        (answer_for("internal"), "INTERNAL", ""),
        (answer_for("Private"), "", ""),
        (answer_for("PRIVATE"), "", ""),
        (answer_for(3), "", ""),
        (answer_for("private", "tbhb-dev/other"), "", OTHER),
        (answer_for("private", None), "", OTHER),
        (reply(200, {"visibility": "private"}), "", OTHER),
        (reply(200, ["private"]), "", NO_ANSWER),
        (reply(404, {"visibility": "private"}), "", NO_ANSWER),
        (httpx.ConnectError("down"), "", UNREADABLE),
        (ValueError("not json"), "", UNREADABLE),
    ],
)
def test_the_scan_visibility_comes_from_the_repository_read(
    monkeypatch: pytest.MonkeyPatch,
    answer: httpx.Response | Exception,
    expected: str,
    logged: str,
) -> None:
    def get(*_args: object, **_options: object) -> httpx.Response:
        if isinstance(answer, Exception):
            raise answer
        return answer

    lines: list[tuple[str, str]] = []
    monkeypatch.setattr(httpx, "get", get)
    result = open_pr.repository_visibility(
        NAMED, "tok", lambda suffix, text: lines.append((suffix, text))
    )
    assert result == expected
    assert lines == ([("log", logged)] if logged else [])

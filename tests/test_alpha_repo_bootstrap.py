# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

"""Tests for tbhbdev alpha repo bootstrap, with gh faked at subprocess.run."""

import argparse
import json
import os
import re
import stat
import subprocess
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from repo_settings_example import DEFAULTS, EXAMPLE

from tbhbdev import alpha, gh_agent, host
from tbhbdev.alpha import repo_bootstrap as rb
from tbhbdev.alpha import repo_settings as rs

if TYPE_CHECKING:
    from conftest import Installed

REPO = "example-org/example-repo"
CONFIG = EXAMPLE
# Settings that already match CONFIG for REPO.
GOOD = {**DEFAULTS, "has_issues": True}
# What GitHub gives a new repository, with fields the command must never send.
FRESH = {
    "allow_squash_merge": True,
    "allow_merge_commit": True,
    "allow_rebase_merge": True,
    "squash_merge_commit_title": "COMMIT_OR_PR_TITLE",
    "squash_merge_commit_message": "COMMIT_MESSAGES",
    "delete_branch_on_merge": False,
    "has_issues": True,
    "has_wiki": True,
    "has_projects": True,
}
REAL_NOW = rb.now
NOW = datetime(2026, 11, 9, 13, 30, 5, tzinfo=UTC)
PROG = "tbhbdev alpha repo"
ALLOWED_CALLS = (
    ("GET", re.compile(r"repos/[^/]+/[^/]+\Z")),
    ("PATCH", re.compile(r"repos/[^/]+/[^/]+\Z")),
    ("POST", re.compile(r"orgs/[^/]+/repos\Z")),
)
NOT_FOUND = "gh: Not Found (HTTP 404)\n"


@dataclass
class Call:
    argv: list[str]
    stdin: str
    env: dict[str, str]
    kwargs: dict[str, object]

    @property
    def method(self) -> str:
        return self.argv[3]

    @property
    def path(self) -> str:
        return self.argv[4]


@dataclass
class FakeGh:
    """gh api over a dict of repositories; it records every call."""

    repos: dict[str, dict[str, object]] = field(default_factory=dict)
    # Keys a PATCH silently ignores, as GitHub does on some plans.
    ignored: tuple[str, ...] = ()
    # (method, path) -> (exit code, stderr) for calls that fail.
    fail: dict[tuple[str, str], tuple[int, str]] = field(default_factory=dict)
    stdout: dict[tuple[str, str], str] = field(default_factory=dict)
    calls: list[Call] = field(default_factory=list)

    def of(self, method: str) -> list[Call]:
        return [call for call in self.calls if call.method == method]

    def __call__(
        self, argv: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        env = kwargs["env"]
        assert isinstance(env, dict)
        stdin = kwargs["input"]
        assert isinstance(stdin, str)
        call = Call(list(argv), stdin, dict(env), kwargs)
        self.calls.append(call)
        key = (call.method, call.path)
        if key in self.fail:
            code, err = self.fail[key]
            return subprocess.CompletedProcess(argv, code, "", err)
        if key in self.stdout:
            return subprocess.CompletedProcess(argv, 0, self.stdout[key], "")
        name = call.path.removeprefix("repos/")
        if call.method != "POST" and name not in self.repos:
            return subprocess.CompletedProcess(argv, 1, "", NOT_FOUND)
        return subprocess.CompletedProcess(argv, 0, self.reply(call), "")

    def reply(self, call: Call) -> str:
        if call.method == "POST":
            body = json.loads(call.stdin)
            org = call.path.split("/")[1]
            self.repos[f"{org}/{body['name']}"] = {
                **FRESH,
                "name": body["name"],
                "visibility": body["visibility"],
                "private": body["visibility"] == "private",
                "default_branch": "main",
            }
            return "{}"
        name = call.path.removeprefix("repos/")
        if call.method == "PATCH":
            body = json.loads(call.stdin)
            self.repos[name].update(
                {k: v for k, v in body.items() if k not in self.ignored}
            )
        return json.dumps(self.repos[name])


def existing(**changes: object) -> dict[str, object]:
    return {
        **GOOD,
        "name": "example-repo",
        "visibility": "private",
        "private": True,
        "default_branch": "main",
        **changes,
    }


@pytest.fixture(autouse=True)
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    monkeypatch.setattr(rb, "now", lambda: NOW)
    return tmp_path


@pytest.fixture
def plugin(installed: Installed) -> None:
    installed.stub("example-plugin", files={rs.CONFIG_FILE: CONFIG})


def gh_for(monkeypatch: pytest.MonkeyPatch, **kwargs: object) -> FakeGh:
    fake = FakeGh(**kwargs)  # type: ignore[arg-type]
    monkeypatch.setattr(subprocess, "run", fake)
    return fake


def log_lines(home: Path) -> list[dict[str, object]]:
    path = home / ".local" / "state" / "tbhbdev" / "repo-bootstrap.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines()]


def run(capsys: pytest.CaptureFixture[str], *args: str) -> tuple[int, str, str]:
    code = rb.main([*args], prog=PROG)
    out, err = capsys.readouterr()
    return code, out, err


# ------------------------------------------------------------------ config


def test_no_plugin_config_fails_closed_before_any_call(
    home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = gh_for(monkeypatch)
    code, out, err = run(capsys, "bootstrap", REPO)
    assert (code, out) == (1, "")
    assert err == (
        f"{PROG}: no installed plugin supplies the config file repo-settings.toml\n"
    )
    assert fake.calls == []
    assert log_lines(home) == []


def test_a_plugin_that_supplies_nothing_for_it_fails_closed(
    installed: Installed,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    installed.stub("example-plugin", files={"other.toml": CONFIG})
    fake = gh_for(monkeypatch)
    code, _, err = run(capsys, "bootstrap", REPO)
    assert code == 1
    assert "repo-settings.toml" in err
    assert fake.calls == []


def test_an_invalid_config_names_the_file_and_key_and_calls_nothing(
    installed: Installed,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    text = '[defaults]\nvisibility = "public"\n'
    installed.stub("example-plugin", files={rs.CONFIG_FILE: text})
    fake = gh_for(monkeypatch)
    code, out, err = run(capsys, "bootstrap", REPO)
    assert (code, out) == (1, "")
    assert err.startswith(f"{PROG}: repo-settings.toml: [defaults] visibility is not ")
    assert err.count("\n") == 1
    assert fake.calls == []


def test_two_plugins_supplying_it_is_an_error(
    installed: Installed,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    installed.stub("one", files={rs.CONFIG_FILE: CONFIG})
    installed.stub("two", files={rs.CONFIG_FILE: CONFIG})
    fake = gh_for(monkeypatch)
    code, _, err = run(capsys, "bootstrap", REPO)
    assert code == 1
    assert err == f"{PROG}: plugins 'one', 'two' each supply repo-settings.toml\n"
    assert fake.calls == []


# ---------------------------------------------------------------- bootstrap


@pytest.mark.usefixtures("plugin")
def test_up_to_date_prints_it_and_makes_one_get_and_no_log(
    home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = gh_for(monkeypatch, repos={REPO: existing()})
    code, out, err = run(capsys, "bootstrap", REPO)
    assert (code, out, err) == (0, f"{REPO}: up to date\n", "")
    assert [(c.method, c.path) for c in fake.calls] == [("GET", f"repos/{REPO}")]
    assert log_lines(home) == []
    assert not (home / ".local").exists()


@pytest.mark.usefixtures("plugin")
def test_changes_are_sent_as_one_patch_of_the_differing_keys_then_verified(
    home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    drifted = existing(
        allow_merge_commit=True, has_wiki=True, delete_branch_on_merge=False
    )
    fake = gh_for(monkeypatch, repos={REPO: drifted})
    code, out, err = run(capsys, "bootstrap", REPO)
    assert (code, err) == (0, "")
    assert out == (
        f"{REPO}: updated\n"
        "allow_merge_commit: true -> false\n"
        "delete_branch_on_merge: false -> true\n"
        "has_wiki: true -> false\n"
        "3 changes\n"
    )
    assert [(c.method, c.path) for c in fake.calls] == [
        ("GET", f"repos/{REPO}"),
        ("PATCH", f"repos/{REPO}"),
        ("GET", f"repos/{REPO}"),
    ]
    patch = fake.of("PATCH")[0]
    assert json.loads(patch.stdin) == {
        "allow_merge_commit": False,
        "delete_branch_on_merge": True,
        "has_wiki": False,
    }
    assert patch.argv == [
        "gh",
        "api",
        "--method",
        "PATCH",
        f"repos/{REPO}",
        "--input",
        "-",
    ]
    assert log_lines(home) == [
        {
            "v": 1,
            "ts": "2026-11-09T13:30:05Z",
            "repo": REPO,
            "created": False,
            "changed": {
                "allow_merge_commit": [True, False],
                "delete_branch_on_merge": [False, True],
                "has_wiki": [True, False],
            },
            "verified": True,
        }
    ]


@pytest.mark.usefixtures("plugin")
def test_the_repos_table_wins_over_the_defaults(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    other = "example-org/other"
    fake = gh_for(
        monkeypatch,
        repos={REPO: existing(has_issues=False), other: existing(has_issues=True)},
    )
    assert run(capsys, "bootstrap", REPO)[0] == 0
    assert json.loads(fake.of("PATCH")[0].stdin) == {"has_issues": True}
    assert run(capsys, "bootstrap", other)[0] == 0
    assert json.loads(fake.of("PATCH")[1].stdin) == {"has_issues": False}


@pytest.mark.usefixtures("plugin")
def test_a_changed_squash_message_sends_its_title_too(
    home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = gh_for(
        monkeypatch,
        repos={REPO: existing(squash_merge_commit_message="PR_BODY")},
    )
    assert run(capsys, "bootstrap", REPO)[0] == 0
    assert json.loads(fake.of("PATCH")[0].stdin) == {
        "squash_merge_commit_title": "PR_TITLE",
        "squash_merge_commit_message": "BLANK",
    }
    assert list(log_lines(home)[0]["changed"]) == ["squash_merge_commit_message"]  # type: ignore[call-overload]


@pytest.mark.usefixtures("plugin")
def test_a_key_the_reply_lacks_is_sent(
    home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = existing()
    del repo["has_wiki"]
    fake = gh_for(monkeypatch, repos={REPO: repo})
    assert run(capsys, "bootstrap", REPO)[0] == 0
    assert json.loads(fake.of("PATCH")[0].stdin) == {"has_wiki": False}
    assert log_lines(home)[0]["changed"] == {"has_wiki": [None, False]}


@pytest.mark.usefixtures("plugin")
def test_a_key_that_does_not_hold_exits_1_names_it_and_logs_it_unverified(
    home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = gh_for(
        monkeypatch,
        repos={REPO: existing(has_wiki=True, has_projects=True)},
        ignored=("has_projects",),
    )
    code, out, err = run(capsys, "bootstrap", REPO)
    assert (code, out) == (1, "")
    assert err == f"{PROG}: {REPO}: still differs after the update: has_projects\n"
    assert len(fake.of("PATCH")) == 1
    assert len(fake.of("GET")) == 2
    entry = log_lines(home)[0]
    assert entry["verified"] is False
    assert entry["changed"] == {
        "has_wiki": [True, False],
        "has_projects": [True, False],
    }


@pytest.mark.usefixtures("plugin")
def test_a_failed_patch_exits_1_and_logs_nothing(
    home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    gh_for(
        monkeypatch,
        repos={REPO: existing(has_wiki=True)},
        fail={("PATCH", f"repos/{REPO}"): (1, "gh: Forbidden (HTTP 403)\n")},
    )
    code, out, err = run(capsys, "bootstrap", REPO)
    assert (code, out) == (1, "")
    assert err == (
        f"{PROG}: gh api PATCH repos/{REPO} failed (exit 1): gh: Forbidden (HTTP 403)\n"
    )
    assert log_lines(home) == []


@pytest.mark.usefixtures("plugin")
def test_a_failed_verifying_read_exits_1(
    home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = gh_for(monkeypatch, repos={REPO: existing(has_wiki=True)})
    real = fake.__call__
    seen: list[str] = []

    def second_get_fails(
        argv: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        seen.append(argv[3])
        if seen.count("GET") == 2:
            return subprocess.CompletedProcess(
                argv, 1, "", "gh: Bad Gateway (HTTP 502)\n"
            )
        return real(argv, **kwargs)

    monkeypatch.setattr(subprocess, "run", second_get_fails)
    code, _, err = run(capsys, "bootstrap", REPO)
    assert code == 1
    assert "HTTP 502" in err
    assert log_lines(home) == []


# ------------------------------------------------------------------ dry run


@pytest.mark.usefixtures("plugin")
def test_dry_run_prints_before_and_after_and_only_reads(
    home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = gh_for(monkeypatch, repos={REPO: existing(has_wiki=True, has_issues=False)})
    code, out, err = run(capsys, "bootstrap", "--dry-run", REPO)
    assert (code, err) == (0, "")
    assert out == "has_issues: false -> true\nhas_wiki: true -> false\n2 changes\n"
    assert [(c.method, c.path) for c in fake.calls] == [("GET", f"repos/{REPO}")]
    assert not (home / ".local").exists()


@pytest.mark.usefixtures("plugin")
def test_dry_run_with_nothing_to_change(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    gh_for(monkeypatch, repos={REPO: existing()})
    assert run(capsys, "bootstrap", "--dry-run", REPO) == (0, "0 changes\n", "")


# ------------------------------------------------------------------- create


@pytest.mark.usefixtures("plugin")
def test_a_missing_repo_without_create_exits_1_and_suggests_it(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = gh_for(monkeypatch)
    code, out, err = run(capsys, "bootstrap", REPO)
    assert (code, out) == (1, "")
    assert err == (
        f"{PROG}: {REPO} does not exist; to create it, add --create with "
        "--private or --public\n"
    )
    assert [c.method for c in fake.calls] == ["GET"]


@pytest.mark.usefixtures("plugin")
@pytest.mark.parametrize("flag", ["--private", "--public"])
def test_create_makes_the_repo_then_applies_the_settings(
    flag: str,
    home: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    fake = gh_for(monkeypatch)
    code, out, err = run(capsys, "bootstrap", "--create", flag, REPO)
    assert (code, err) == (0, "")
    assert out.startswith(f"{REPO}: created\n")
    assert [(c.method, c.path) for c in fake.calls] == [
        ("GET", f"repos/{REPO}"),
        ("POST", "orgs/example-org/repos"),
        ("GET", f"repos/{REPO}"),
        ("PATCH", f"repos/{REPO}"),
        ("GET", f"repos/{REPO}"),
    ]
    visibility = flag.removeprefix("--")
    post = fake.of("POST")[0]
    assert json.loads(post.stdin) == {"name": "example-repo", "visibility": visibility}
    assert post.argv == [
        "gh",
        "api",
        "--method",
        "POST",
        "orgs/example-org/repos",
        "--input",
        "-",
    ]
    assert fake.repos[REPO]["visibility"] == visibility
    assert {k: fake.repos[REPO][k] for k in GOOD} == GOOD
    entry = log_lines(home)[0]
    assert entry["created"] is True
    assert entry["verified"] is True
    assert set(entry["changed"]) == {  # type: ignore[call-overload]
        "allow_merge_commit",
        "allow_rebase_merge",
        "squash_merge_commit_title",
        "squash_merge_commit_message",
        "delete_branch_on_merge",
        "has_wiki",
        "has_projects",
    }


@pytest.mark.usefixtures("plugin")
def test_create_dry_run_writes_nothing(
    home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = gh_for(monkeypatch)
    code, out, err = run(
        capsys, "bootstrap", "--dry-run", "--create", "--private", REPO
    )
    assert (code, err) == (0, "")
    assert out == (
        f"would create {REPO} (private)\n"
        "settings it would get:\n"
        "allow_squash_merge: true\n"
        "allow_merge_commit: false\n"
        "allow_rebase_merge: false\n"
        'squash_merge_commit_title: "PR_TITLE"\n'
        'squash_merge_commit_message: "BLANK"\n'
        "delete_branch_on_merge: true\n"
        "has_issues: true\n"
        "has_wiki: false\n"
        "has_projects: false\n"
    )
    assert [c.method for c in fake.calls] == ["GET"]
    assert not (home / ".local").exists()


@pytest.mark.usefixtures("plugin")
def test_an_existing_repo_with_another_visibility_exits_1_and_changes_nothing(
    home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = gh_for(monkeypatch, repos={REPO: existing(has_wiki=True)})
    code, out, err = run(capsys, "bootstrap", "--create", "--public", REPO)
    assert (code, out) == (1, "")
    assert err == f"{PROG}: {REPO} exists and is private, not public; nothing changed\n"
    assert [c.method for c in fake.calls] == ["GET"]
    assert log_lines(home) == []


@pytest.mark.usefixtures("plugin")
def test_an_existing_repo_with_the_same_visibility_carries_on(
    home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = gh_for(monkeypatch, repos={REPO: existing(has_wiki=True)})
    code, out, _ = run(capsys, "bootstrap", "--create", "--private", REPO)
    assert code == 0
    assert out.startswith(f"{REPO}: updated\n")
    assert fake.of("POST") == []
    assert len(fake.of("PATCH")) == 1
    assert log_lines(home)[0]["created"] is False


@pytest.mark.usefixtures("plugin")
def test_a_failed_create_exits_1_and_logs_nothing(
    home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    gh_for(
        monkeypatch,
        fail={("POST", "orgs/example-org/repos"): (1, "gh: Not Found (HTTP 404)\n")},
    )
    code, _, err = run(capsys, "bootstrap", "--create", "--private", REPO)
    assert code == 1
    assert err == (
        f"{PROG}: gh api POST orgs/example-org/repos failed (exit 1): "
        "gh: Not Found (HTTP 404)\n"
    )
    assert log_lines(home) == []


@pytest.mark.usefixtures("plugin")
def test_a_created_repo_is_logged_even_when_the_update_fails(
    home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    gh_for(
        monkeypatch,
        fail={("PATCH", f"repos/{REPO}"): (1, "gh: Forbidden (HTTP 403)\n")},
    )
    code, _, err = run(capsys, "bootstrap", "--create", "--private", REPO)
    assert code == 1
    assert "HTTP 403" in err
    entry = log_lines(home)[0]
    assert entry["repo"] == REPO
    assert entry["created"] is True
    assert entry["verified"] is False
    assert entry["changed"] != {}


def test_a_created_repo_with_nothing_to_change_is_logged_without_a_patch(
    installed: Installed,
    home: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    installed.stub("example-plugin", files={rs.CONFIG_FILE: "[defaults]\n"})
    fake = gh_for(monkeypatch)
    code, out, _ = run(capsys, "bootstrap", "--create", "--public", REPO)
    assert (code, out) == (0, f"{REPO}: created\n0 changes\n")
    assert fake.of("PATCH") == []
    assert log_lines(home) == [
        {
            "v": 1,
            "ts": "2026-11-09T13:30:05Z",
            "repo": REPO,
            "created": True,
            "changed": {},
            "verified": True,
        }
    ]


# ------------------------------------------------------------ gh failures


@pytest.mark.usefixtures("plugin")
@pytest.mark.parametrize(
    ("code", "stderr", "suggests_create"),
    [
        (1, NOT_FOUND, True),
        (1, "gh: Not Found (HTTP 404)\nextra line\n", True),
        (1, "gh: Forbidden (HTTP 403)\n", False),
        (1, "gh: Internal Server Error (HTTP 500)\n", False),
        (4, NOT_FOUND, False),
        (2, "", False),
        (1, "gh: Not Found (HTTP 405)\n", False),
    ],
)
def test_a_404_is_told_from_other_gh_failures_by_exit_code_and_stderr(
    code: int,
    stderr: str,
    suggests_create: bool,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    gh_for(monkeypatch, fail={("GET", f"repos/{REPO}"): (code, stderr)})
    status, out, err = run(capsys, "bootstrap", REPO)
    assert (status, out) == (1, "")
    assert ("--create" in err) is suggests_create
    assert err.count("\n") == 1
    if not suggests_create:
        why = " ".join(stderr.split())
        tail = f" {why}" if why else ""
        assert err == f"{PROG}: gh api GET repos/{REPO} failed (exit {code}):{tail}\n"


@pytest.mark.usefixtures("plugin")
def test_a_404_on_create_is_not_the_missing_repo_case(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = gh_for(
        monkeypatch,
        fail={("POST", "orgs/example-org/repos"): (1, NOT_FOUND)},
    )
    code, _, err = run(capsys, "bootstrap", "--create", "--private", REPO)
    assert code == 1
    assert "failed (exit 1)" in err
    assert [c.method for c in fake.calls] == ["GET", "POST"]


@pytest.mark.usefixtures("plugin")
def test_gh_missing_exits_1(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def missing(*_args: object, **_kwargs: object) -> None:
        raise FileNotFoundError(2, "No such file or directory", "gh")

    monkeypatch.setattr(subprocess, "run", missing)
    code, out, err = run(capsys, "bootstrap", REPO)
    assert (code, out) == (1, "")
    assert err == f"{PROG}: cannot run gh: [Errno 2] No such file or directory: 'gh'\n"


@pytest.mark.usefixtures("plugin")
@pytest.mark.parametrize(
    ("stdout", "why"),
    [
        ("not json", "replied with something that isn't JSON"),
        ("[]", "replied with something that isn't an object"),
        ('"x"', "replied with something that isn't an object"),
    ],
)
def test_an_unusable_reply_exits_1(
    stdout: str,
    why: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    gh_for(monkeypatch, stdout={("GET", f"repos/{REPO}"): stdout})
    code, _, err = run(capsys, "bootstrap", REPO)
    assert code == 1
    assert err == f"{PROG}: gh api GET repos/{REPO} {why}\n"


# --------------------------------------------------------------- the calls


@pytest.mark.usefixtures("plugin")
@pytest.mark.parametrize(
    "args",
    [
        ["bootstrap", REPO],
        ["bootstrap", "--dry-run", REPO],
        ["bootstrap", "--create", "--private", REPO],
        ["bootstrap", "--create", "--public", "--dry-run", REPO],
    ],
)
@pytest.mark.parametrize("state", ["missing", "drifted", "good"])
def test_only_get_and_patch_on_the_repo_and_post_on_the_org_are_ever_called(
    args: list[str],
    state: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repos = {
        "missing": dict[str, dict[str, object]](),
        "drifted": {REPO: existing(has_wiki=True)},
        "good": {REPO: existing()},
    }[state]
    fake = gh_for(monkeypatch, repos=repos)
    rb.main(args, prog=PROG)
    capsys.readouterr()
    assert fake.calls
    for call in fake.calls:
        assert call.argv[:3] == ["gh", "api", "--method"]
        assert any(
            call.method == method and pattern.fullmatch(call.path)
            for method, pattern in ALLOWED_CALLS
        ), (call.method, call.path)
        assert (
            re.search(r"ruleset|protection|collaborator|team|hook|branches", call.path)
            is None
        )
    if "--dry-run" in args:
        assert {c.method for c in fake.calls} == {"GET"}


@pytest.mark.usefixtures("plugin")
def test_gh_runs_with_the_gh_agent_dir_and_the_rest_of_the_environment(
    home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("GH_CONFIG_DIR", "/example/other-config")
    monkeypatch.setenv("EXAMPLE_VAR", "example-value")
    fake = gh_for(monkeypatch, repos={REPO: existing(has_wiki=True)})
    assert run(capsys, "bootstrap", REPO)[0] == 0
    expected = {**os.environ, "GH_CONFIG_DIR": str(home / ".config" / "gh-agent")}
    for call in fake.calls:
        assert call.env == expected
        assert call.kwargs["capture_output"] is True
        assert call.kwargs["text"] is True
        assert call.kwargs["check"] is False
        # Nothing secret or environmental is on argv.
        assert call.argv[:3] == ["gh", "api", "--method"]
        assert len(call.argv) <= 7
    assert [c.stdin for c in fake.of("GET")] == ["", ""]


def test_gh_env_overrides_only_the_config_dir() -> None:
    env = {"A": "1", "GH_CONFIG_DIR": "/example/x"}
    assert rb.gh_env(env, Path("/example/home")) == {
        "A": "1",
        "GH_CONFIG_DIR": "/example/home/.config/gh-agent",  # gitleaks:allow
    }
    assert env["GH_CONFIG_DIR"] == "/example/x"


def test_the_gh_agent_dir_is_the_one_host_uses() -> None:
    assert gh_agent.CONFIG_DIR == ".config/gh-agent"
    assert host.GH_AGENT == 'GH_CONFIG_DIR="$HOME/.config/gh-agent"'  # gitleaks:allow


# --------------------------------------------------------------------- log


@pytest.mark.usefixtures("plugin")
def test_the_log_is_private_and_appended_to(
    home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    old = os.umask(0)
    try:
        gh_for(monkeypatch, repos={REPO: existing(has_wiki=True)})
        assert run(capsys, "bootstrap", REPO)[0] == 0
        directory = home / ".local" / "state" / "tbhbdev"
        path = directory / "repo-bootstrap.jsonl"
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        rb_fake = gh_for(monkeypatch, repos={REPO: existing(has_projects=True)})
        assert run(capsys, "bootstrap", REPO)[0] == 0
        assert len(rb_fake.of("PATCH")) == 1
    finally:
        os.umask(old)
    lines = path.read_text().splitlines()
    assert len(lines) == 2
    assert [json.loads(line)["changed"] for line in lines] == [
        {"has_wiki": [True, False]},
        {"has_projects": [True, False]},
    ]
    assert path.read_text().endswith("\n")


@pytest.mark.usefixtures("plugin")
def test_the_log_follows_xdg_state_home(
    home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    state = home / "example-state"
    monkeypatch.setenv("XDG_STATE_HOME", str(state))
    gh_for(monkeypatch, repos={REPO: existing(has_wiki=True)})
    assert run(capsys, "bootstrap", REPO)[0] == 0
    assert (state / "tbhbdev" / "repo-bootstrap.jsonl").is_file()
    assert not (home / ".local").exists()


def test_log_path_defaults_under_home(home: Path) -> None:
    assert (
        rb.log_path({})
        == home / ".local" / "state" / "tbhbdev" / "repo-bootstrap.jsonl"
    )
    assert rb.log_path({"XDG_STATE_HOME": ""}) == (
        home / ".local" / "state" / "tbhbdev" / "repo-bootstrap.jsonl"
    )
    assert rb.log_path({"XDG_STATE_HOME": "/example/s"}) == Path(
        "/example/s/tbhbdev/repo-bootstrap.jsonl"
    )


@pytest.mark.usefixtures("plugin")
def test_the_log_holds_no_token_environment_or_argv(
    home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("GH_TOKEN", "example-token-value")
    gh_for(monkeypatch, repos={REPO: existing(has_wiki=True)})
    assert run(capsys, "bootstrap", REPO)[0] == 0
    path = home / ".local" / "state" / "tbhbdev" / "repo-bootstrap.jsonl"
    text = path.read_text()
    assert "example-token-value" not in text
    assert set(json.loads(text)) == {
        "v",
        "ts",
        "repo",
        "created",
        "changed",
        "verified",
    }


@pytest.mark.usefixtures("plugin")
def test_a_log_that_cannot_be_written_exits_1(
    home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    state = home / "example-state"
    state.write_text("not a directory")
    monkeypatch.setenv("XDG_STATE_HOME", str(state))
    gh_for(monkeypatch, repos={REPO: existing(has_wiki=True)})
    code, _, err = run(capsys, "bootstrap", REPO)
    assert code == 1
    assert err.startswith(f"{PROG}: ")
    assert err.count("\n") == 1


def test_now_is_utc_and_current() -> None:
    before = datetime.now(UTC)
    moment = REAL_NOW()
    assert moment.tzinfo is UTC
    assert before <= moment <= datetime.now(UTC)


# -------------------------------------------------------------- the command


@pytest.mark.parametrize(
    "args",
    [
        [],
        ["bootstrap"],
        ["bootstrap", "nope"],
        ["bootstrap", "a/b/c"],
        ["bootstrap", REPO, "--create"],
        ["bootstrap", REPO, "--private"],
        ["bootstrap", REPO, "--public"],
        ["bootstrap", REPO, "--create", "--private", "--public"],
        ["bootstrap", REPO, "--unknown"],
        ["other", REPO],
    ],
)
def test_usage_errors_exit_2_and_call_nothing(
    args: list[str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    fake = gh_for(monkeypatch)
    with pytest.raises(SystemExit) as caught:
        rb.main(args, prog=PROG)
    assert caught.value.code == 2
    assert capsys.readouterr().err.startswith(f"usage: {PROG}")
    assert fake.calls == []


def test_the_visibility_rules_are_messages(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        rb.main(["bootstrap", REPO, "--create"], prog=PROG)
    assert f"{PROG}: error: --create requires --private or --public\n" in (
        capsys.readouterr().err
    )
    with pytest.raises(SystemExit):
        rb.main(["bootstrap", REPO, "--public"], prog=PROG)
    assert f"{PROG}: error: --private and --public require --create\n" in (
        capsys.readouterr().err
    )


def test_alpha_dispatches_repo_to_bootstrap(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert alpha.COMMANDS["repo"] is alpha.repo_command
    fake = gh_for(monkeypatch)
    assert alpha.main(["repo", "bootstrap", REPO], "tbhbdev alpha") == 1
    assert "no installed plugin supplies the config file repo-settings.toml" in (
        capsys.readouterr().err
    )
    assert fake.calls == []


def test_alpha_usage_lists_repo() -> None:
    usage = alpha.USAGE.format(prog="tbhbdev alpha")
    head = "\n  repo         bootstrap [--dry-run] [--create --private|--public]"
    assert f"{head} ORG/REPO:\n               apply the standard" in usage


def test_repo_name_refuses_what_is_not_org_slash_repo() -> None:
    with pytest.raises(argparse.ArgumentTypeError) as caught:
        rb.repo_name("nope")
    assert str(caught.value) == "must be ORG/REPO"
    assert rb.repo_name(REPO) == REPO


def test_a_bad_repo_name_is_a_usage_error_that_says_what_it_wants(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit):
        rb.main(["bootstrap", "nope"], prog=PROG)
    assert "argument ORG/REPO: must be ORG/REPO" in capsys.readouterr().err


def test_the_parser_names_its_command_and_arguments() -> None:
    parser = rb.build_parser(PROG)
    assert parser.format_help().startswith(f"usage: {PROG} [-h] {{bootstrap}} ...")
    assert "\nRepository tools.\n" in parser.format_help()
    assert " apply the standard repository settings\n" in parser.format_help()
    args = parser.parse_args(["bootstrap", REPO])
    assert args.verb == "bootstrap"


def test_the_bootstrap_help_shows_the_repo_argument(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as caught:
        rb.main(["bootstrap", "--help"], prog=PROG)
    assert caught.value.code == 0
    out = capsys.readouterr().out
    assert f"usage: {PROG} bootstrap [-h] [--dry-run] [--create]" in out
    assert "ORG/REPO" in out


def test_create_repo_splits_at_the_first_slash(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = gh_for(monkeypatch)
    rb.create_repo("example-org/example-repo", "public")
    assert fake.calls[0].path == "orgs/example-org/repos"
    assert json.loads(fake.calls[0].stdin) == {
        "name": "example-repo",
        "visibility": "public",
    }
    rb.create_repo("a/b/c", "public")
    assert fake.calls[1].path == "orgs/a/repos"
    assert json.loads(fake.calls[1].stdin)["name"] == "b/c"


@pytest.mark.usefixtures("plugin")
def test_two_keys_that_do_not_hold_are_both_named(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    gh_for(
        monkeypatch,
        repos={REPO: existing(has_wiki=True, has_projects=True)},
        ignored=("has_wiki", "has_projects"),
    )
    _, _, err = run(capsys, "bootstrap", REPO)
    assert (
        err
        == f"{PROG}: {REPO}: still differs after the update: has_wiki, has_projects\n"
    )

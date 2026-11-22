"""Push checks against disposable repositories and a bare remote."""

from __future__ import annotations

import io
import os
import subprocess
import sys
from pathlib import Path

import pytest
from fake_tool import pythonpath

from tbhbdev import alpha
from tbhbdev.alpha import install
from tbhbdev.alpha import pre_push_guard as pg

ZERO = "0" * 40
PUBLIC = "https://github.com/example/public.git"
PRIVATE = "fixturerepo"


def git(path: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *pg.SAFE, *args],
        cwd=path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for name in [key for key in os.environ if key.startswith("GIT_")]:
        monkeypatch.delenv(name, raising=False)
    for name in (
        "HOME",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "XDG_STATE_HOME",
        "XDG_CACHE_HOME",
        "XDG_RUNTIME_DIR",
    ):
        monkeypatch.setenv(name, str(tmp_path / name.lower()))
    for name in ("SKIP", "PREK_SKIP"):
        monkeypatch.delenv(name, raising=False)
    for role in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{role}_NAME", "Example")
        monkeypatch.setenv(f"GIT_{role}_EMAIL", "example@example.test")
    remote = tmp_path / "remote.git"
    remote.mkdir()
    git(remote, "init", "--bare", "-q")
    path = tmp_path / "repo"
    path.mkdir()
    git(path, "init", "-q")
    git(path, "config", "commit.gpgsign", "false")
    git(path, "remote", "add", "origin", str(remote))
    git(path, "commit", "--allow-empty", "-qm", "initial")
    git(path, "push", "origin", "HEAD:refs/heads/main")
    monkeypatch.chdir(path)
    return path


def commit(repo: Path, message: str = "next", **files: str) -> str:
    for name, content in files.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    git(repo, "add", ".")
    git(repo, "commit", "--allow-empty", "-qm", message)
    return git(repo, "rev-parse", "HEAD")


def guard(
    monkeypatch: pytest.MonkeyPatch, local: str, remote: str = ZERO, url: str = PUBLIC
) -> int:
    monkeypatch.setattr(
        sys,
        "stdin",
        io.StringIO(f"refs/heads/topic {local} refs/heads/topic {remote}\n"),
    )
    return alpha.main(["pre-push-guard", "origin", url], "tbhbdev alpha")


@pytest.mark.parametrize("kind", ["diverged", "forward", "new", "delete", "missing"])
def test_ancestry(
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    kind: str,
) -> None:
    base = git(repo, "rev-parse", "HEAD")
    head = commit(repo)
    local, remote = {
        "diverged": (base, head),
        "forward": (head, base),
        "new": (head, ZERO),
        "delete": (ZERO, base),
        "missing": (head, "f" * 40),
    }[kind]
    denied = kind in {"diverged", "missing"}
    assert guard(monkeypatch, local, remote) == int(denied)
    out, err = capsys.readouterr()
    assert out == ""
    if denied:
        assert (
            err == "refs/heads/topic: non-fast-forward push refused; "
            "push a new branch instead\n"
        )
    else:
        assert err == ""


@pytest.mark.parametrize("variable", ["SKIP", "PREK_SKIP"])
@pytest.mark.parametrize("value", ["", "sensitive-value", "0"])
def test_skip_refused(
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    variable: str,
    value: str,
) -> None:
    monkeypatch.setenv(variable, value)
    assert guard(monkeypatch, commit(repo)) == int(bool(value))
    assert capsys.readouterr() == (
        "",
        f"pre-push-guard: {variable} is set; push refused\n" if value else "",
    )


@pytest.mark.parametrize("ref", ["refs/heads/main", "refs/heads/master"])
@pytest.mark.parametrize("kind", ["forward", "new", "delete"])
def test_protected_branch_refused(
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    ref: str,
    kind: str,
) -> None:
    base = git(repo, "rev-parse", "HEAD")
    head = commit(repo)
    local, remote = {
        "forward": (head, base),
        "new": (head, ZERO),
        "delete": (ZERO, base),
    }[kind]
    monkeypatch.setattr(sys, "stdin", io.StringIO(f"HEAD {local} {ref} {remote}\n"))
    assert alpha.main(["pre-push-guard", "origin", PUBLIC], "tbhbdev alpha") == 1
    out, err = capsys.readouterr()
    assert out == ""
    assert err == (
        f"{ref}: push to a protected branch refused; "
        "push a new branch and open a pull request\n"
    )


def test_protected_branch_refused_beside_other_refs(
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    base = git(repo, "rev-parse", "HEAD")
    head = commit(repo)
    monkeypatch.setattr(
        sys,
        "stdin",
        io.StringIO(
            f"refs/heads/topic {head} refs/heads/topic {ZERO}\n"
            f"refs/heads/topic {head} refs/heads/main {base}\n"
        ),
    )
    assert alpha.main(["pre-push-guard", "origin", PUBLIC], "tbhbdev alpha") == 1
    assert capsys.readouterr().err.startswith("refs/heads/main: push to a protected")


def test_protected_branch_does_not_hide_later_refs(
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    base = git(repo, "rev-parse", "HEAD")
    head = commit(repo)
    monkeypatch.setattr(
        sys,
        "stdin",
        io.StringIO(
            f"refs/heads/topic {head} refs/heads/main {base}\n"
            f"refs/heads/topic {base} refs/heads/topic {head}\n"
        ),
    )
    assert alpha.main(["pre-push-guard", "origin", PUBLIC], "tbhbdev alpha") == 1
    err = capsys.readouterr().err
    assert "refs/heads/main: push to a protected" in err
    assert "refs/heads/topic: non-fast-forward push refused" in err


@pytest.mark.parametrize(
    "ref",
    [
        "refs/heads/maintenance",
        "refs/heads/main2",
        "refs/tags/main",
        "refs/heads/x/main",
    ],
)
def test_similar_names_are_not_protected(
    repo: Path, monkeypatch: pytest.MonkeyPatch, ref: str
) -> None:
    head = commit(repo)
    monkeypatch.setattr(sys, "stdin", io.StringIO(f"HEAD {head} {ref} {ZERO}\n"))
    assert alpha.main(["pre-push-guard", "origin", PUBLIC], "tbhbdev alpha") == 0


@pytest.mark.parametrize("default", ["upstream", "simple", "current"])
def test_tracking_main_is_refused_for_any_push_default(
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    default: str,
) -> None:
    git(repo, "checkout", "-q", "-b", "topic", "origin/main")
    git(repo, "config", "push.default", default)
    assert git(repo, "config", "branch.topic.merge") == "refs/heads/main"
    head = commit(repo)
    base = git(repo, "rev-parse", "origin/main")
    monkeypatch.setattr(
        sys,
        "stdin",
        io.StringIO(f"refs/heads/topic {head} refs/heads/main {base}\n"),
    )
    assert alpha.main(["pre-push-guard", "origin", PUBLIC], "tbhbdev alpha") == 1
    assert "refs/heads/main: push to a protected" in capsys.readouterr().err


def test_help_uses_supplied_program(
    plain: None, capsys: pytest.CaptureFixture[str]
) -> None:
    del plain
    assert pg.main(["--help"], "example guard") == 1
    assert capsys.readouterr() == ("", "usage: example guard REMOTE URL\n")


@pytest.mark.parametrize(
    "url",
    [
        f"https://github.com/example/{PRIVATE}.git",
        f"https://example.test/{PRIVATE}",
        "",
    ],
)
def test_private_repository_names_pass_everywhere(
    repo: Path, monkeypatch: pytest.MonkeyPatch, url: str
) -> None:
    # A repository name in the message, a path and a line is public text now.
    # The list file that once held the names isn't read.
    head = commit(repo, PRIVATE, **{PRIVATE: f"{PRIVATE}\n"})
    assert guard(monkeypatch, head, url=url) == 0


def test_bad_input_is_refused(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys, "stdin", io.StringIO("broken\n"))
    assert alpha.main(["pre-push-guard", "origin", PUBLIC], "p") == 1
    assert capsys.readouterr() == (
        "",
        "pre-push-guard: cannot inspect push; push refused\n",
    )


def test_duplicate_and_empty_input(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    head = commit(repo)
    row = f"refs/heads/topic {head} refs/heads/topic {ZERO}\n"
    monkeypatch.setattr(sys, "stdin", io.StringIO(row + row))
    assert alpha.main(["pre-push-guard", "origin", PUBLIC], "p") == 0
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    assert alpha.main(["pre-push-guard", "origin", PUBLIC], "p") == 0
    assert capsys.readouterr() == ("", "")


def test_a_malformed_old_object_is_a_rewrite(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert guard(monkeypatch, commit(repo), "X" * 40) == 1


def test_wrong_argument_count(capsys: pytest.CaptureFixture[str]) -> None:
    assert pg.main(["origin"], "example guard") == 1
    assert capsys.readouterr() == ("", "usage: example guard REMOTE URL\n")


def test_hook_refuses_a_push_to_main(repo: Path) -> None:
    hooks = repo / ".git/hooks"
    (hooks / "pre-push").write_text("#!/bin/sh\n# File generated by prek\nexit 0\n")
    (hooks / "pre-push").chmod(0o755)
    install.install_pre_push_hook(hooks)
    binary = repo / "bin"
    binary.mkdir()
    tool = binary / "tbhbdev"
    tool.write_text(
        f"#!{sys.executable}\n"
        "import sys\nfrom tbhbdev.cli import main\nsys.exit(main())\n"
    )
    tool.chmod(0o755)
    child_env = dict(os.environ)
    child_env.pop("MUTANT_UNDER_TEST", None)
    child_env["PATH"] = f"{binary}:{os.environ['PATH']}"
    child_env["PYTHONPATH"] = pythonpath(Path(pg.__file__).parents[2])
    allowed = subprocess.run(
        ["git", "push", "origin", "HEAD:refs/heads/allowed"],
        cwd=repo,
        env=child_env,
        capture_output=True,
        check=False,
    )
    assert allowed.returncode == 0
    commit(repo, "next")
    done = subprocess.run(
        ["git", "push", "origin", "HEAD:refs/heads/main"],
        env=child_env,
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode != 0
    assert "protected branch" in done.stderr, done.stderr
    assert "Traceback" not in done.stderr

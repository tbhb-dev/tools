"""Tests for tbhbdev.alpha.repo: the origin check and the hook's directory."""

import io
import subprocess
import sys
from pathlib import Path

import pytest
from origin_urls import OTHER_URLS, TBHB_DEV_URLS

from tbhbdev.alpha import repo


@pytest.mark.parametrize("url", TBHB_DEV_URLS)
def test_tbhb_dev_urls(url: str) -> None:
    assert repo.is_tbhb_dev_url(url)


@pytest.mark.parametrize("url", OTHER_URLS)
def test_other_urls(url: str) -> None:
    assert not repo.is_tbhb_dev_url(url)


def init(path: Path, origin: str | None) -> Path:
    path.mkdir()
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    if origin is not None:
        subprocess.run(
            ["git", "-C", str(path), "remote", "add", "origin", origin], check=True
        )
    return path


@pytest.fixture(autouse=True)
def isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    for who in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{who}_NAME", "T")
        monkeypatch.setenv(f"GIT_{who}_EMAIL", "t@example.com")


def test_in_tbhb_dev(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ours = init(tmp_path / "ours", "https://github.com/tbhb-dev/x")
    assert repo.in_tbhb_dev(ours)
    (ours / "sub").mkdir()
    assert repo.in_tbhb_dev(ours / "sub")
    monkeypatch.chdir(ours / "sub")
    assert repo.in_tbhb_dev()
    assert not repo.in_tbhb_dev(init(tmp_path / "theirs", "git@github.com:a/x"))
    assert not repo.in_tbhb_dev(init(tmp_path / "none", None))
    (tmp_path / "plain").mkdir()
    assert not repo.in_tbhb_dev(tmp_path / "plain")
    assert not repo.in_tbhb_dev(tmp_path / "missing")


def test_in_tbhb_dev_in_a_linked_worktree(tmp_path: Path) -> None:
    ours = init(tmp_path / "ours", "git@github.com:tbhb-dev/x.git")
    subprocess.run(
        ["git", "-C", str(ours), "commit", "-q", "--allow-empty", "-m", "x"],
        check=True,
    )
    tree = tmp_path / "tree"
    subprocess.run(
        ["git", "-C", str(ours), "worktree", "add", "-q", str(tree)], check=True
    )
    assert repo.in_tbhb_dev(tree)


def test_git(tmp_path: Path) -> None:
    assert repo.git("--version")[0] == 0
    assert repo.git("--version")[1].startswith("git version ")
    assert not repo.git("--version")[1].endswith("\n")
    assert repo.git("rev-parse", cwd=tmp_path)[0] == 128
    assert repo.git("--version", cwd=tmp_path / "missing") == (128, "")


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        ({"cwd": "/a"}, Path("/a")),
        ({"cwd": "/a", "workspacePaths": ["/b"]}, Path("/a")),
        ({"cwd": "", "workspacePaths": ["/b", "/c"]}, Path("/b")),
        ({"cwd": 3, "workspacePaths": ["/b"]}, Path("/b")),
        ({"workspacePaths": []}, None),
        ({"workspacePaths": [""]}, None),
        ({"workspacePaths": [3]}, None),
        ({"workspacePaths": "/b"}, None),
        ({}, None),
    ],
)
def test_hook_cwd(data: dict[str, object], expected: Path | None) -> None:
    assert repo.hook_cwd(data) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ('{"a": 1}', {"a": 1}),
        ("", {}),
        ("not json", {}),
        ("[1]", {}),
        ("null", {}),
    ],
)
def test_read_hook_stdin(
    monkeypatch: pytest.MonkeyPatch, text: str, expected: dict[str, object]
) -> None:
    monkeypatch.setattr(sys, "stdin", io.StringIO(text))
    assert repo.read_hook_stdin() == expected

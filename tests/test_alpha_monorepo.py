"""Small real repositories exercise the monorepo commands without a network."""

import subprocess
import tomllib
from typing import TYPE_CHECKING, cast

import pytest

from tbhbdev import alpha
from tbhbdev.alpha import monorepo
from tbhbdev.alpha.monorepo import source

if TYPE_CHECKING:
    from pathlib import Path

PROG = "tbhbdev alpha monorepo"

ORIGIN = "https://github.com/tbhb-dev/tools.git"

DEST = "factory/tbhbdev"

APP = b"print('hello')\n"

AGENTS = b"Read tbhb-dev/tools for the rules.\n"

SOURCE_FILES: dict[str, bytes] = {
    "README.md": b"Clone https://github.com/tbhb-dev/tools as a record.\n",
    "src/app.py": APP,
    "bin/run.sh": b"#!/bin/sh\necho run\n",
    "AGENTS.md": AGENTS,
    "uv.lock": b"lock\n",
    ".github/workflows/ci.yml": b"uses: tbhb-dev/rules/x.yml@abc\n",
    "LICENSES/a.txt": b"license\n",
}


def git(path: Path, *args: str, data: bytes | None = None) -> str:
    return subprocess.run(
        ["git", "-C", str(path), *args],
        input=data,
        capture_output=True,
        check=True,
    ).stdout.decode()


def commit_all(path: Path, message: str = "commit") -> str:
    git(path, "add", "-A")
    git(path, "commit", "-q", "-m", message)
    return git(path, "rev-parse", "HEAD").strip()


@pytest.fixture(autouse=True)
def plain_git(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for role in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{role}_NAME", "Example")
        monkeypatch.setenv(f"GIT_{role}_EMAIL", "example@example.com")


def write_tree(path: Path, tree: dict[str, bytes]) -> None:
    for name, data in tree.items():
        target = path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    (path / "bin/run.sh").chmod(0o755)
    (path / "link").symlink_to("src/app.py")


@pytest.fixture
def src(tmp_path: Path) -> Src:
    path = tmp_path / "source"
    path.mkdir()
    git(path, "init", "-q")
    git(path, "remote", "add", "origin", ORIGIN)
    write_tree(path, SOURCE_FILES)
    return path, commit_all(path)


type Src = tuple[Path, str]


type Capsys = pytest.CaptureFixture[str]


def run(*argv: str) -> int:
    return monorepo.main(list(argv), PROG)


def table(text: str) -> dict[str, object]:
    return tomllib.loads(text)


def test_map_copies_and_drops(src: Src, capsys: Capsys) -> None:
    path, commit = src
    assert run("map", str(path), "HEAD", "--dest", f"{DEST}/") == 0
    out = capsys.readouterr().out
    data = table(out)
    assert data["repo"] == "tbhb-dev/tools"
    assert data["commit"] == commit
    assert data["dest"] == DEST
    assert data["rewrite"] == {}
    copied = cast("dict[str, str]", data["copy"])
    assert copied["src/app.py"] == f"{DEST}/src/app.py"
    assert copied["link"] == f"{DEST}/link"
    assert copied["README.md"] == f"{DEST}/README.md"
    dropped = cast("dict[str, str]", data["drop"])
    assert set(dropped) == {"uv.lock", ".github/workflows/ci.yml", "LICENSES/a.txt"}
    assert dropped["uv.lock"] == "root configuration (uv.lock)"


def test_map_takes_a_repo_and_needs_a_github_origin(src: Src, capsys: Capsys) -> None:
    path, _ = src
    git(path, "remote", "set-url", "origin", "/srv/git/tools")
    assert run("map", str(path), "HEAD", "--dest", DEST) == 2
    assert "pass --repo OWNER/NAME" in capsys.readouterr().err
    assert run("map", str(path), "HEAD", "--dest", DEST, "--repo", "a/b") == 0
    assert table(capsys.readouterr().out)["repo"] == "a/b"


def test_map_writes_the_file_it_is_asked_for(
    src: Src, tmp_path: Path, capsys: Capsys
) -> None:
    path, commit = src
    target = tmp_path / "map.toml"
    assert run("map", str(path), commit, "--dest", DEST, "-o", str(target)) == 0
    assert capsys.readouterr().out == ""
    assert table(target.read_text())["commit"] == commit


@pytest.mark.parametrize("bad", ["", "/abs", "a/../b", "a//b", ".git", "x/.GIT/y"])
def test_map_refuses_a_bad_dest(src: Src, capsys: Capsys, bad: str) -> None:
    path, _ = src
    assert run("map", str(path), "HEAD", "--dest", bad) == 2
    assert "bad path" in capsys.readouterr().err


def test_map_refuses_submodules_and_odd_paths(src: Src, capsys: Capsys) -> None:
    path, _ = src
    oid = git(path, "rev-parse", "HEAD").strip()
    git(path, "update-index", "--add", "--cacheinfo", f"160000,{oid},sub")
    git(path, "commit", "-q", "-m", "sub")
    assert run("map", str(path), "HEAD", "--dest", DEST) == 2
    assert "sub is a commit" in capsys.readouterr().err
    git(path, "rm", "-q", "--cached", "sub")
    blob = git(path, "hash-object", "-w", "--stdin", data=b"x").strip()
    odd = f"100644,{blob},bad".encode() + b"\xff"
    subprocess.run(
        ["git", "-C", str(path), "update-index", "--add", "--cacheinfo", odd],
        check=True,
    )
    git(path, "commit", "-q", "-m", "odd")
    assert run("map", str(path), "HEAD", "--dest", DEST) == 2
    assert "isn't UTF-8" in capsys.readouterr().err


def test_git_reports_a_missing_git_and_a_failed_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(source.MonorepoError, match="not a git repository"):
        source.git(str(tmp_path), "status")
    monkeypatch.setenv("PATH", "")
    with pytest.raises(source.MonorepoError):
        source.git(str(tmp_path), "status")


def test_the_command_line_parses_and_dispatches(
    capsys: Capsys,
) -> None:
    assert run("--help") == 0
    assert "import a public repository" not in capsys.readouterr().err
    assert run() == 2
    capsys.readouterr()
    assert alpha.main(["monorepo", "-h"], "tbhbdev alpha") == 0
    assert "monorepo" in alpha.USAGE

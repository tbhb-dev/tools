"""Small real repositories exercise map, import and verify without a network."""

import os
import subprocess
import tomllib
from pathlib import Path
from typing import cast

import pytest

from tbhbdev import alpha
from tbhbdev.alpha import monorepo
from tbhbdev.alpha.monorepo import pathmap, source, verify

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


@pytest.fixture
def mono(tmp_path: Path) -> Path:
    path = tmp_path / "mono"
    path.mkdir()
    git(path, "init", "-q")
    (path / "README.md").write_text("monorepo\n")
    commit_all(path, "chore: start")
    return path


type Src = tuple[Path, str]
type Capsys = pytest.CaptureFixture[str]


def run(*argv: str) -> int:
    return monorepo.main(list(argv), PROG)


def run_import(source: str, made: Path, mono: Path) -> int:
    return run("import", source, "--map", str(made), "--into", str(mono))


def make_map(src: Src, tmp_path: Path, *, edit: dict[str, str] | None = None) -> Path:
    path, commit = src
    target = tmp_path / "map.toml"
    assert run("map", str(path), commit, "--dest", DEST, "-o", str(target)) == 0
    text = target.read_text()
    for old, new in (edit or {}).items():
        assert old in text
        text = text.replace(old, new)
    target.write_text(text)
    return target


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
    assert pathmap.load(out).drop == dropped


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


def test_import_copies_byte_for_byte_in_one_commit(
    src: Src,
    mono: Path,
    tmp_path: Path,
    capsys: Capsys,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path, commit = src
    made = make_map(src, tmp_path)
    before = git(mono, "rev-parse", "HEAD").strip()
    monkeypatch.setattr(source, "BATCH", 2)
    assert run_import(f"{path}@{commit}", made, mono) == 0
    assert "imported 5 files from tbhb-dev/tools at" in capsys.readouterr().out
    assert (mono / DEST / "src/app.py").read_bytes() == APP
    assert os.access(mono / DEST / "bin/run.sh", os.X_OK)
    assert not os.access(mono / DEST / "src/app.py", os.X_OK)
    assert (mono / DEST / "link").readlink() == Path("src/app.py")
    assert not (mono / DEST / "uv.lock").exists()
    assert git(mono, "rev-list", "--count", f"{before}..HEAD").strip() == "1"
    assert git(mono, "rev-parse", "HEAD~").strip() == before
    assert git(mono, "status", "--porcelain") == ""
    message = git(mono, "log", "-1", "--format=%B")
    assert message.startswith(f"feat({DEST}): import tools at {commit[:7]}\n")
    assert f"Source repository: tbhb-dev/tools\nSource commit: {commit}\n" in message
    assert "Copied 5 files, rewrote 0 and dropped 3." in message
    assert verify_exit(src, made, mono) == 0
    assert "verify: 0 problems" in capsys.readouterr().out


def verify_exit(src: Src, made: Path, mono: Path) -> int:
    path, commit = src
    return run("verify", f"{path}@{commit}", "--map", str(made), "--tree", str(mono))


def rewritten(
    src: Src,
    tmp_path: Path,
    old: str = "tbhb-dev/tools",
    name: str = "AGENTS.md",
) -> Path:
    text = make_map(src, tmp_path).read_text()
    text = text.replace(f'"{name}" = "{DEST}/{name}"\n', "")
    text = text.replace(
        "[rewrite]\n",
        f'[rewrite."{name}"]\ndest = "{DEST}/{name}"\n'
        f'replace = [["{old}", "tbhb-dev/mono/{DEST}"]]\n',
    )
    path = tmp_path / "rewrite.toml"
    path.write_text(text)
    return path


def test_rewrite_applies_replacements_and_verify_recomputes_them(
    src: Src, mono: Path, tmp_path: Path, capsys: Capsys
) -> None:
    path, commit = src
    made = rewritten(src, tmp_path)
    assert run_import(f"{path}@{commit}", made, mono) == 0
    assert (mono / DEST / "AGENTS.md").read_bytes() == (
        b"Read tbhb-dev/mono/factory/tbhbdev for the rules.\n"
    )
    assert "rewrote 1" in git(mono, "log", "-1", "--format=%B")
    capsys.readouterr()
    assert verify_exit(src, made, mono) == 0
    (mono / DEST / "AGENTS.md").write_bytes(AGENTS)
    assert verify_exit(src, made, mono) == 1
    assert f"{DEST}/AGENTS.md: differs from AGENTS.md" in capsys.readouterr().out


def test_a_stale_replacement_leaves_nothing_behind(
    src: Src, mono: Path, tmp_path: Path, capsys: Capsys
) -> None:
    path, commit = src
    made = rewritten(src, tmp_path, old="nowhere", name="src/app.py")
    before = git(mono, "rev-parse", "HEAD")
    assert run_import(f"{path}@{commit}", made, mono) == 2
    assert (
        "src/app.py: replacement 'nowhere' isn't in the file" in capsys.readouterr().err
    )
    assert git(mono, "rev-parse", "HEAD") == before
    assert not (mono / DEST / "src/app.py").exists()
    assert not (mono / DEST / "AGENTS.md").exists()
    assert verify_exit(src, made, mono) == 1
    assert "replacement 'nowhere'" in capsys.readouterr().out


def import_error(src: Src, made: Path, mono: Path, capsys: Capsys) -> str:
    path, commit = src
    assert run_import(f"{path}@{commit}", made, mono) == 2
    return capsys.readouterr().err


def test_import_refuses_unsafe_starting_points(
    src: Src, mono: Path, tmp_path: Path, capsys: Capsys
) -> None:
    made = make_map(src, tmp_path)
    (mono / "new.txt").write_text("x")
    assert "has uncommitted changes" in import_error(src, made, mono, capsys)
    commit_all(mono)
    (mono / DEST).mkdir(parents=True)
    (mono / DEST / "README.md").write_text("mine")
    commit_all(mono)
    assert f"already in {mono}: {DEST}/README.md" in import_error(
        src, made, mono, capsys
    )


def test_import_refuses_a_bad_source_or_map(
    src: Src, mono: Path, tmp_path: Path, capsys: Capsys
) -> None:
    path, commit = src
    made = make_map(src, tmp_path)
    for bad in ("nothing", f"{path}@"):
        assert run_import(bad, made, mono) == 2
        assert "is not CLONE@COMMIT" in capsys.readouterr().err
    path.joinpath("later.txt").write_bytes(b"x\n")
    later = commit_all(path)
    assert run_import(f"{path}@{later}", made, mono) == 2
    assert f"the map is for {commit}, not {later}" in capsys.readouterr().err


def test_import_refuses_a_map_that_misses_files(
    src: Src, mono: Path, tmp_path: Path, capsys: Capsys
) -> None:
    made = make_map(
        src, tmp_path, edit={'"uv.lock" = "root configuration (uv.lock)"\n': ""}
    )
    assert "uv.lock: source file is not in the map" in import_error(
        src, made, mono, capsys
    )


def test_import_refuses_a_long_subject_and_an_empty_import(
    src: Src, mono: Path, tmp_path: Path, capsys: Capsys
) -> None:
    long = "f" * 60
    made = make_map(src, tmp_path)
    text = made.read_text().replace(DEST, f"factory/{long}")
    made.write_text(text)
    assert "the subject is over 72 characters" in import_error(src, made, mono, capsys)


def test_import_does_not_write_through_a_symlinked_folder(
    src: Src, mono: Path, tmp_path: Path, capsys: Capsys
) -> None:
    made = make_map(src, tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (mono / "factory").mkdir()
    (mono / "factory/tbhbdev").symlink_to(outside)
    commit_all(mono)
    assert "would leave factory/tbhbdev" in import_error(src, made, mono, capsys)
    assert not list(outside.iterdir())


def test_verify_reports_each_kind_of_difference(
    src: Src, mono: Path, tmp_path: Path, capsys: Capsys
) -> None:
    path, commit = src
    made = make_map(src, tmp_path)
    assert run_import(f"{path}@{commit}", made, mono) == 0
    capsys.readouterr()
    folder = mono / DEST
    (folder / "src/app.py").write_bytes(b"changed\n")
    (folder / "README.md").unlink()
    (folder / "bin/run.sh").chmod(0o644)
    (folder / "link").unlink()
    (folder / "link").write_text("not a link")
    (folder / "AGENTS.md").unlink()
    (folder / "AGENTS.md").mkdir()
    assert verify_exit(src, made, mono) == 1
    out = capsys.readouterr().out
    assert f"{DEST}/src/app.py: differs from src/app.py" in out
    assert f"{DEST}/README.md: missing from the tree" in out
    assert f"{DEST}/bin/run.sh: mode 100644, source has 100755" in out
    assert f"{DEST}/link: mode 100644, source has 120000" in out
    assert f"{DEST}/AGENTS.md: missing from the tree" in out
    assert "verify: 5 problems" in out


def test_verify_requires_the_map_to_account_for_every_file(
    src: Src, tmp_path: Path, capsys: Capsys
) -> None:
    made = make_map(
        src,
        tmp_path,
        edit={
            '"uv.lock" = "root configuration (uv.lock)"\n': "",
            "[drop]\n": '[drop]\n"ghost" = "never existed"\n',
        },
    )
    assert verify_exit(src, made, tmp_path) == 1
    out = capsys.readouterr().out
    assert "uv.lock: source file is not in the map" in out
    assert "ghost: in the map but not in the source" in out


def test_verify_reports_executable_references(
    tmp_path: Path, mono: Path, capsys: Capsys
) -> None:
    path = tmp_path / "old"
    path.mkdir()
    git(path, "init", "-q")
    git(path, "remote", "add", "origin", ORIGIN)
    tree = {
        "bin/run.sh": b"git clone git@github.com:tbhb-dev/tools.git\n",
        "ci/a.yml": b"  - uses: 'tbhb-dev/rules/.github/workflows/x.yml@v1'\n",
        "renovate.json5": b"",
        "ci/renovate.json": b'{"extends": ["github>tbhb-dev/renovate-config"]}\n',
        "MODULE.bazel.txt": b"",
        "deps/MODULE.bazel": b'git_override(remote = "https://github.com/tbhb-dev/tools")\n',
        "go.mod": b"module go.example.dev/tools\n",
        "main.go": b'import "go.example.dev/tools/pkg"\n',
        "fine.go": b'import "go.example.dev/toolbox"\nuses: actions/checkout@abc\n',
        "docs/NOTE.md": b"see github.com/tbhb-dev/tools\n",
        "vendor/tools": b"github.com/tbhb-dev/toolset\nx\n",
    }
    for name, data in tree.items():
        (path / name).parent.mkdir(parents=True, exist_ok=True)
        (path / name).write_bytes(data)
    (path / "link").symlink_to("github.com/tbhb-dev/tools")
    commit = commit_all(path)
    made = tmp_path / "map.toml"
    assert run("map", str(path), commit, "--dest", DEST, "-o", str(made)) == 0
    assert run_import(f"{path}@{commit}", made, mono) == 0
    capsys.readouterr()
    assert (
        run("verify", f"{path}@{commit}", "--map", str(made), "--tree", str(mono)) == 1
    )
    out = capsys.readouterr().out.splitlines()
    assert out == [
        f"{DEST}/bin/run.sh:1: address of the source repository",
        f"{DEST}/ci/a.yml:1: workflow uses an old repository",
        f"{DEST}/ci/renovate.json:1: Renovate preset from an old repository",
        f"{DEST}/deps/MODULE.bazel:1: address of the source repository",
        f"{DEST}/go.mod:1: Go path go.example.dev/tools",
        f"{DEST}/main.go:1: Go path go.example.dev/tools",
        "verify: 6 problems",
    ]


def test_references_skip_prose_lines_of_other_files_still_count() -> None:
    patterns = verify.rules("o/n", set())
    assert list(verify.references("a.md", b"github.com/o/n\n", patterns)) == []
    assert list(verify.references("a.sh", b"ok\ngithub.com/o/n\n", patterns)) == [
        "a.sh:2: address of the source repository"
    ]


def load_error(text: str) -> str:
    with pytest.raises(source.MonorepoError) as caught:
        pathmap.load(text)
    return str(caught.value)


HEAD = 'repo = "o/n"\ncommit = "abc"\ndest = "d"\n'


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("= nope", "not valid TOML"),
        ('commit = "a"\ndest = "d"\n', "map value repo must be a string"),
        ('repo = "n"\ncommit = "a"\ndest = "d"\n', "must be OWNER/NAME"),
        (HEAD + "copy = 1\n", "map key 'copy' must be a table"),
        (HEAD + "[copy]\na = 1\n", "map value a must be a string"),
        (HEAD + '[copy]\na = "d/a"\n[drop]\na = "x"\n', "more than one table"),
        (HEAD + '[copy]\na = "d/x"\nb = "d/x"\n', "writes one path twice"),
        (HEAD + '[copy]\na = "e/a"\n', "bad path 'e/a'"),
        (
            HEAD + '[rewrite.a]\ndest = "d/a"\nreplace = [["x", 2]]\n',
            "list of [old, new]",
        ),
        (HEAD + "[rewrite.a]\n", "map value a dest must be a string"),
    ],
)
def test_load_refuses_a_malformed_map(text: str, message: str) -> None:
    assert message in load_error(text)


def test_load_accepts_a_rewrite_without_replacements() -> None:
    made = pathmap.load(HEAD + '[rewrite.a]\ndest = "d/a"\n')
    assert made.rewrite["a"] == pathmap.Rewrite("d/a", [])


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
    assert run("verify") == 2
    capsys.readouterr()
    assert alpha.main(["monorepo", "-h"], "tbhbdev alpha") == 0
    assert "monorepo" in alpha.USAGE

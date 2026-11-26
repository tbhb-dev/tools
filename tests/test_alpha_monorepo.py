"""Small real repositories exercise map, import and verify without a network."""

import os
import shutil
import subprocess
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest

from tbhbdev import alpha
from tbhbdev.alpha import monorepo
from tbhbdev.alpha.monorepo import source, verify
from tbhbdev.alpha.monorepo.pathmap import Rewrite, dump, load

if TYPE_CHECKING:
    from collections.abc import Callable

PROG = "tbhbdev alpha monorepo"
ORIGIN = "https://github.com/tbhb-dev/tools.git"
DEST = "factory/tbhbdev"
APP = b"print('hello')\n"
WORKFLOW = "workflow uses an old repository"
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
    assert load(out).drop == dropped


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


def rewritten(src: Src, tmp_path: Path, old: str, name: str) -> Path:
    entry = f'"{name}" = "{DEST}/{name}"\n'
    head = f'[rewrite."{name}"]\ndest = "{DEST}/{name}"\n'
    return make_map(
        src,
        tmp_path,
        edit={entry: "", "[rewrite]\n": f'{head}replace = [["{old}", "x"]]\n'},
    )


def test_rewrite_applies_replacements_and_verify_recomputes_them(
    src: Src, mono: Path, tmp_path: Path, capsys: Capsys
) -> None:
    path, commit = src
    made = rewritten(src, tmp_path, "tbhb-dev/tools", "AGENTS.md")
    assert run_import(f"{path}@{commit}", made, mono) == 0
    assert (mono / DEST / "AGENTS.md").read_bytes() == (b"Read x for the rules.\n")
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


def test_import_refuses_a_long_subject(
    src: Src, mono: Path, tmp_path: Path, capsys: Capsys
) -> None:
    made = make_map(src, tmp_path, edit={DEST: f"factory/{'f' * 60}"})
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


@dataclass
class Imported:
    source: Src
    tree: Path
    mapping: Path
    capsys: Capsys

    def problems(self) -> list[str]:
        assert verify_exit(self.source, self.mapping, self.tree) == 1
        return self.capsys.readouterr().out.splitlines()


@pytest.fixture
def imported(src: Src, mono: Path, capsys: Capsys) -> Imported:
    path, commit = src
    made = make_map(src, mono.parent)
    assert run_import(f"{path}@{commit}", made, mono) == 0
    capsys.readouterr()
    return Imported(src, mono, made, capsys)


def test_verify_reports_each_kind_of_difference(mono: Path, imported: Imported) -> None:
    folder = mono / DEST
    (folder / "src/app.py").unlink()
    os.mkfifo(folder / "src/app.py")
    (folder / "bin/run.sh").chmod(0o644)
    (folder / "link").unlink()
    (folder / "link").write_text("not a link")
    (folder / "AGENTS.md").unlink()
    (folder / "AGENTS.md").mkdir()
    out = "\n".join(imported.problems())
    assert f"{DEST}/src/app.py: missing from the tree" in out
    assert f"{DEST}/bin/run.sh: mode 100644, source has 100755" in out
    assert f"{DEST}/link: mode 100644, source has 120000" in out
    assert f"{DEST}/AGENTS.md: missing from the tree" in out
    assert "verify: 4 problems" in out


@pytest.mark.parametrize("target", ["./src/app.py", "src//app.py", "src/app.py/"])
def test_verify_rejects_changed_symlink_target(imported: Imported, target: str) -> None:
    link = imported.tree / DEST / "link"
    link.unlink()
    link.symlink_to(target)
    assert imported.problems() == [
        f"{DEST}/link: differs from link",
        "verify: 1 problems",
    ]


@pytest.mark.parametrize("key", ['"uses"', "'uses'", "uses"])
@pytest.mark.parametrize("indicator", [">2-", "|2-", ">-2", "|+2"])
def test_references_parse_yaml_uses(key: str, indicator: str) -> None:
    data = (
        "jobs:\n  check:\n    steps:\n"
        f"      - {key}: {indicator}\n          o/action@v1\n"
    ).encode()
    assert list(
        verify.references("ci.yaml", data, verify.rules("o/n", set(), set()), "o/n")
    ) == ["ci.yaml:4: workflow uses an old repository"]


@pytest.mark.parametrize("address", ["https://github.com/", "git@github.com:"])
def test_references_detect_source_repository_names(address: str) -> None:
    data = f"git clone {address}tbhb-dev/ansible.git\n".encode()
    assert list(
        verify.references("clone.sh", data, verify.rules("o/n", set(), set()), "o/n")
    ) == ["clone.sh:1: address of an old repository"]


@pytest.mark.parametrize(
    "extra", ["surprise.txt", "uv.lock", "src/app.py", ".hidden", "nested/stray.txt"]
)
def test_verify_rejects_files_not_produced_by_the_map(
    mono: Path, imported: Imported, extra: str
) -> None:
    mapping = load(imported.mapping.read_text())
    mapping.copy["src/app.py"] = f"{DEST}/moved.py"
    imported.mapping.write_text(dump(mapping))
    (mono / DEST / "src/app.py").rename(mono / DEST / "moved.py")
    target = mono / DEST / extra
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(APP)
    assert imported.problems() == [
        f"{DEST}/{extra}: not produced by the map",
        "verify: 1 problems",
    ]


@pytest.mark.parametrize("component", [".", "factory", DEST, f"{DEST}/src"])
def test_verify_rejects_symlinked_destination_folders(
    mono: Path,
    imported: Imported,
    monkeypatch: pytest.MonkeyPatch,
    component: str,
) -> None:
    folder = mono / component
    outside = mono.parent / "outside"
    folder.rename(outside)
    folder.symlink_to(outside, target_is_directory=True)
    original = Path.read_bytes

    def read_bytes(target: Path) -> bytes:
        assert not target.is_relative_to(folder)
        return original(target)

    monkeypatch.setattr(Path, "read_bytes", read_bytes)
    expected = (
        f"{DEST}/src: not produced by the map"
        if component == f"{DEST}/src"
        else f"{folder}: symbolic link in destination path"
    )
    assert expected in imported.problems()


@pytest.mark.parametrize("missing_table", ["copy", "rewrite", "drop"])
def test_verify_collects_map_gaps_and_other_differences(
    mono: Path, imported: Imported, missing_table: str
) -> None:
    mapping = load(imported.mapping.read_text())
    del mapping.copy["link"]
    del mapping.drop["uv.lock"]
    if missing_table == "copy":
        mapping.copy["ghost"] = f"{DEST}/ghost"
    elif missing_table == "rewrite":
        mapping.rewrite["ghost"] = Rewrite(f"{DEST}/ghost", [])
    else:
        mapping.drop["ghost"] = "absent"
    for name in ("AGENTS.md", "README.md"):
        del mapping.copy[name]
        mapping.rewrite[name] = Rewrite(f"{DEST}/{name}", [("missing text", "new")])
    imported.mapping.write_text(dump(mapping))
    (mono / DEST / "ghost").write_bytes(b"not in the source\n")
    (mono / DEST / "src/app.py").write_bytes(b"changed\n")
    (mono / DEST / "bin/run.sh").unlink()
    assert imported.problems() == [
        "link: source file is not in the map",
        "uv.lock: source file is not in the map",
        "ghost: in the map but not in the source",
        f"{DEST}/ghost: not produced by the map",
        f"{DEST}/link: not produced by the map",
        "AGENTS.md: replacement 'missing text' isn't in the file",
        "README.md: replacement 'missing text' isn't in the file",
        f"{DEST}/bin/run.sh: missing from the tree",
        f"{DEST}/src/app.py: differs from src/app.py",
        "verify: 9 problems",
    ]


def test_verify_rejects_file_instead_of_destination_folder(
    mono: Path, imported: Imported
) -> None:
    shutil.rmtree(mono / DEST)
    (mono / DEST).write_bytes(b"file instead of folder\n")
    out = "\n".join(imported.problems())
    assert f"{DEST}: not produced by the map" in out
    assert "verify: 6 problems" in out


@pytest.mark.parametrize(
    "case",
    [
        ("iterdir", "unreadable", "cannot scan"),
        ("lstat", ".", "cannot scan"),
        ("lstat", "unexpected.txt", "cannot scan"),
        ("lstat", "src/app.py", "cannot scan"),
        ("read_bytes", "AGENTS.md", "cannot read"),
        ("readlink", "link", "cannot read"),
    ],
)
def test_verify_reports_io_errors_and_continues(
    imported: Imported,
    monkeypatch: pytest.MonkeyPatch,
    case: tuple[str, str, str],
) -> None:
    method, target, rule = case
    folder = imported.tree / DEST
    for name in ("unreadable", "also-unreadable"):
        (folder / name).mkdir()
        (folder / name / "hidden").write_bytes(b"hidden")
    (folder / "unexpected.txt").write_bytes(b"extra")
    (folder / "bin/run.sh").chmod(0o644)
    failed = {folder / target, folder / "also-unreadable"}
    owner = os if method == "readlink" else Path
    original = cast("Callable[[Path], object]", getattr(owner, method))

    def fail(path: Path) -> object:
        if path in failed:
            raise PermissionError(path)
        return original(path)

    monkeypatch.setattr(owner, method, fail)
    out = "\n".join(imported.problems())
    assert f"{folder / target}: {rule}:" in out
    if method == "iterdir":
        assert f"{folder / 'also-unreadable'}: cannot scan:" in out
    if target != ".":
        assert f"{DEST}/bin/run.sh: mode 100644, source has 100755" in out


@pytest.mark.parametrize("indicator", [">", ">-", "|", "|-"])
@pytest.mark.parametrize("repo", ["tools", "rules", "telemetry", "go.tbhb.dev"])
def test_verify_reports_references_to_every_old_repository(
    src: Src, mono: Path, capsys: Capsys, repo: str, indicator: str
) -> None:
    path, _ = src
    tree = {
        "bin/run.sh": f"git clone git@github.com:tbhb-dev/{repo}.git\n".encode(),
        "ci/a.yml": (
            "on: push\njobs:\n  check:\n"
            f"    uses: {indicator}\n"
            "      tbhb-dev/rules/.github/workflows/check.yml@v1\n"
        ).encode(),
        "ci/renovate.json": b'{"extends": ["github>tbhb-dev/renovate-config"]}\n',
        "deps/MODULE.bazel": (
            f'git_override(remote = "https://github.com/tbhb-dev/{repo}")\n'.encode()
        ),
        "go.mod": b"module go.example.dev/tools\n",
        "main.go": b'import "go.example.dev/tools/pkg"\n',
        "fine.go": b'import "go.example.dev/toolbox"\nuses: actions/checkout@abc\n',
        "docs/NOTE.md": b"see github.com/tbhb-dev/tools\n",
        "vendor/tools": b"github.com/tbhb-dev/toolset\n",
        "skip.sh": b"github.com/other/rules\ngithub.com/tbhb-dev/ascm\n",
    }
    for name, data in tree.items():
        (path / name).parent.mkdir(parents=True, exist_ok=True)
        (path / name).write_bytes(data)
    (path / "link").unlink()
    (path / "link").symlink_to("github.com/tbhb-dev/tools")
    commit = commit_all(path)
    made = make_map((path, commit), mono.parent)
    mapping = load(made.read_text())
    del mapping.copy["go.mod"]
    mapping.drop["go.mod"] = "module moved to the monorepo root"
    made.write_text(dump(mapping))
    assert run_import(f"{path}@{commit}", made, mono) == 0
    capsys.readouterr()
    assert Imported((path, commit), mono, made, capsys).problems() == [
        f"{DEST}/bin/run.sh:1: address of an old repository",
        f"{DEST}/ci/a.yml:4: workflow uses an old repository",
        f"{DEST}/ci/renovate.json:1: Renovate preset from an old repository",
        f"{DEST}/deps/MODULE.bazel:1: address of an old repository",
        f"{DEST}/main.go:1: Go path go.example.dev/tools",
        "verify: 5 problems",
    ]


@pytest.mark.parametrize("location", ["go.mod", "testdata/nested/go.mod"])
@pytest.mark.parametrize(
    "module",
    [
        "example.com/fixture",
        "github.com/tbhb-dev/tools",
        "go.example.dev/tools/fixture",
    ],
)
def test_verify_accepts_modules_declared_in_the_imported_tree(
    src: Src, mono: Path, capsys: Capsys, location: str, module: str
) -> None:
    path, _ = src
    (path / "go.mod").write_text("module go.example.dev/tools\n")
    mod = path / location
    mod.parent.mkdir(parents=True, exist_ok=True)
    mod.write_text(f"module {module}\n")
    (mod.parent / "main.go").write_text(f'import "{module}/pkg"\n')
    commit = commit_all(path)
    made = make_map((path, commit), mono.parent)
    if location != "go.mod":
        mapping = load(made.read_text())
        del mapping.copy["go.mod"]
        mapping.drop["go.mod"] = "module moved to the monorepo root"
        made.write_text(dump(mapping))
    assert run_import(f"{path}@{commit}", made, mono) == 0
    capsys.readouterr()
    assert verify_exit((path, commit), made, mono) == 0
    assert capsys.readouterr().out == "verify: 0 problems\n"


@pytest.mark.parametrize(
    "module", ["go.example.dev/tools", "github.com/tbhb-dev/tools"]
)
def test_verify_rejects_old_module_imports_beside_local_fixtures(
    src: Src, mono: Path, capsys: Capsys, module: str
) -> None:
    path, _ = src
    fixture = path / "testdata/nested"
    fixture.mkdir(parents=True)
    (path / "go.mod").write_text(f"module {module}\n")
    (fixture / "go.mod").write_text(f"module {module}/fixture\n")
    (fixture / "main.go").write_text(
        f'import "{module}/fixture/pkg"\n'
        f'import "{module}/fixture-other/pkg"\n'
        f'import "{module}/pkg"\n'
    )
    commit = commit_all(path)
    made = make_map((path, commit), mono.parent)
    mapping = load(made.read_text())
    del mapping.copy["go.mod"]
    mapping.drop["go.mod"] = "module moved to the monorepo root"
    made.write_text(dump(mapping))
    assert run_import(f"{path}@{commit}", made, mono) == 0
    capsys.readouterr()
    rule = (
        "address of an old repository"
        if module.startswith("github.com/")
        else f"Go path {module}"
    )
    assert Imported((path, commit), mono, made, capsys).problems() == [
        f"{DEST}/testdata/nested/main.go:2: {rule}",
        f"{DEST}/testdata/nested/main.go:3: {rule}",
        "verify: 2 problems",
    ]


@pytest.mark.parametrize(
    ("dest", "data", "findings"),
    [
        ("a.md", b"github.com/o/n\n", []),
        ("a.sh", b"ok\ngithub.com/o/n\n", [(2, "address of an old repository")]),
        ("a.sh", b"\ngithub.com/o/n\n", [(2, "address of an old repository")]),
        (
            "ci.yaml",
            b"url: https://github.com/o/n\n",
            [(1, "address of an old repository")],
        ),
        (
            "ci.yaml",
            b"extends: ['github>o/preset']\n",
            [(1, "Renovate preset from an old repository")],
        ),
        ("ci.yml", b"uses: > # folded\r\n\r\n  o/action@v1\r\n", [(1, WORKFLOW)]),
        ("ci.yml", b"uses: |+\n  o/action@v1\n", [(1, WORKFLOW)]),
        ("ci.yml", b"uses:\n  o/action@v1\n", [(1, WORKFLOW)]),
        ("ci.yml", b"uses: >-\n  actions/checkout@v1\n", []),
        ("ci.yml", b"uses: actions/checkout@v1\nname: o/action@v1\n", []),
        ("ci.yaml", b'"uses": o/action@v1\n', [(1, WORKFLOW)]),
        ("ci.yaml", b"'uses': o/action@v1\n", [(1, WORKFLOW)]),
        ("ci.yaml", b'"us\\u0065s": o/action@v1\n', [(1, WORKFLOW)]),
        ("ci.yaml", b"jobs: {check: {uses: 'o/action@v1'}}\n", [(1, WORKFLOW)]),
        ("ci.yaml", b"action: &action o/action@v1\nuses: *action\n", [(2, WORKFLOW)]),
        (
            "ci.yaml",
            b"first: &step {uses: o/action@v1}\nsecond: *step\nloop: &loop [*loop]\n",
            [(1, WORKFLOW)],
        ),
        (
            "ci.yaml",
            b"uses: o/first@v1\n---\nuses: o/second@v1\n",
            [(1, WORKFLOW), (3, WORKFLOW)],
        ),
        ("ci.yaml", b"uses: [actions/checkout@v1]\n? [uses]\n: 1\n", []),
        ("ci.yaml", b"run: |\n  uses: o/action@v1\n", []),
        ("ci.yaml", b"# empty\n", []),
        ("ci.yaml", b"---\n", []),
        ("ci.yaml", b"uses: o/action@v1 # github.com/o/n\n", [(1, WORKFLOW)]),
        ("script.sh", b"uses: o/action@v1\n", [(1, WORKFLOW)]),
        ("snippet.sh", b"uses: > o/action@v1\n", []),
        (
            "snippet.sh",
            b"github.com/o/n github.com/o/n\nuses: o/action@v1 # github.com/o/n\n",
            [(1, "address of an old repository"), (2, WORKFLOW)],
        ),
    ],
)
def test_references(dest: str, data: bytes, findings: list[tuple[int, str]]) -> None:
    assert list(
        verify.references(dest, data, verify.rules("o/n", set(), set()), "o/n")
    ) == [f"{dest}:{line}: {rule}" for line, rule in findings]


@pytest.mark.parametrize("data", [b"uses: > o/action@v1\n", b"uses: [\n", b"\xff"])
def test_references_reject_invalid_yaml(data: bytes) -> None:
    findings = list(
        verify.references("ci.yml", data, verify.rules("o/n", set(), set()), "o/n")
    )
    assert len(findings) == 1
    assert findings[0].startswith("ci.yml: invalid YAML:")


def test_import_into_a_subfolder_commits_the_files(
    src: Src, mono: Path, tmp_path: Path
) -> None:
    path, commit = src
    (mono / "sub").mkdir()
    (mono / "sub/keep.txt").write_text("keep\n")
    commit_all(mono)
    made = make_map(src, tmp_path)
    assert run_import(f"{path}@{commit}", made, mono / "sub") == 0
    listed = git(mono, "ls-tree", "-r", "--name-only", "HEAD").splitlines()
    assert f"{DEST}/src/app.py" in listed
    assert len([n for n in listed if n.startswith(f"{DEST}/")]) == 5
    assert git(mono, "status", "--porcelain") == ""


def untouched(mono: Path, before: str) -> None:
    assert git(mono, "rev-parse", "HEAD") == before
    assert git(mono, "status", "--porcelain") == ""
    assert git(mono, "diff", "--cached", "--name-only") == ""
    assert not (mono / "factory").exists()


def test_an_ignored_destination_rolls_everything_back(
    src: Src, mono: Path, tmp_path: Path, capsys: Capsys
) -> None:
    path, commit = src
    (mono / ".gitignore").write_text("factory/\n")
    commit_all(mono)
    before = git(mono, "rev-parse", "HEAD")
    made = make_map(src, tmp_path)
    assert run_import(f"{path}@{commit}", made, mono) == 2
    assert "git add failed" in capsys.readouterr().err
    untouched(mono, before)
    assert git(mono, "status", "--porcelain", "--ignored") == ""


def test_a_failing_hook_rolls_everything_back(
    src: Src, mono: Path, tmp_path: Path, capsys: Capsys
) -> None:
    path, commit = src
    before = git(mono, "rev-parse", "HEAD")
    hook = mono / ".git/hooks/pre-commit"
    hook.write_text("#!/bin/sh\nexit 1\n")
    hook.chmod(0o755)
    made = make_map(src, tmp_path)
    assert run_import(f"{path}@{commit}", made, mono) == 2
    assert "git commit failed" in capsys.readouterr().err
    untouched(mono, before)
    hook.unlink()
    assert run_import(f"{path}@{commit}", made, mono) == 0


def load_error(text: str) -> str:
    with pytest.raises(source.MonorepoError) as caught:
        load(text)
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
    made = load(HEAD + '[rewrite.a]\ndest = "d/a"\n')
    assert made.rewrite["a"] == Rewrite("d/a", [])


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

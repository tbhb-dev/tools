"""Inventory scripts execute with real POSIX tools and a private tmux stub."""

from __future__ import annotations

import os
import shlex
import subprocess
from typing import TYPE_CHECKING

import pytest

from tbhbdev.alpha.agent.decisions import prune_remote, remote, repo
from tbhbdev.alpha.agent.decisions import runfiles_names as files

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    state = tmp_path / "state" / files.DIRECTORY
    state.mkdir(parents=True)
    (state / "sample.owner").write_text("coordinator\n")
    tools = tmp_path / "bin"
    tools.mkdir()
    tmux = tools / "tmux"
    tmux.write_text(
        "#!/bin/sh\n"
        'test "$1" = -S && test "$3" = -f && test "$4" = /dev/null || exit 9\n'
        'test "$5" = has-session && test "$6" = -t && '
        'test "$7" = =ag-sample || exit 9\n'
        'if test -e "$HOME/running"; then exit 0; fi\n'
        'echo "no server running on private" >&2\nexit 1\n'
    )
    tmux.chmod(0o700)
    monkeypatch.setattr(
        remote, "PREFIX", remote.PREFIX + f"PATH={shlex.quote(str(tools))}:$PATH\n"
    )
    return tmp_path


def execute(host: Path, *, measure: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "/bin/sh",
            "-c",
            prune_remote.facts(
                "example-org/example-repo",
                "sample",
                str(host / "sockets"),
                owner="coordinator",
                measure=measure,
            ),
        ],
        capture_output=True,
        text=True,
        check=False,
    )


def tree_path(host: Path) -> Path:
    return repo.clone_path(host, "example-org/example-repo") / ".worktrees/agent-sample"


def test_inventory_format_and_logical_sizes(host: Path) -> None:
    state = host / "state" / files.DIRECTORY
    for suffix in files.SUFFIXES:
        if suffix != files.OWNER:
            (state / ("sample" + suffix)).write_text("xyz")
    os.utime(state / "sample.rc", (1234567890, 1234567890))
    tree = tree_path(host)
    tree.mkdir(parents=True)
    (tree / "nested").mkdir()
    (tree / "nested/data").write_bytes(b"a" * 31)
    (host / "outside").mkdir()
    (host / "outside/secret").write_bytes(b"x" * 10000)
    (tree / "link").symlink_to(host / "outside", target_is_directory=True)
    (tree / "broken").symlink_to("missing")
    expected = [state / ("sample" + suffix) for suffix in files.SUFFIXES]
    expected += [tree / "nested/data", tree / "link", tree / "broken"]
    result = execute(host)
    assert result.returncode == 0, result.stderr
    assert (
        result.stdout == f"0\n1234567890\n1\n{len(expected)}\n"
        f"{sum(p.lstat().st_size for p in expected)}\n"
    )
    assert result.stderr == ""
    assert (state / "sample.keep").read_text() == "xyz"


@pytest.mark.parametrize("measure", [False, True])
def test_missing_rc_keep_and_clone_and_present_session(
    host: Path, measure: bool
) -> None:
    (host / "running").touch()
    result = execute(host, measure=measure)
    assert result.returncode == 0, result.stderr
    assert result.stdout == ("1\n-\n0\n1\n12\n" if measure else "1\n-\n0\n-\n-\n")


@pytest.mark.parametrize("suffix", files.SUFFIXES)
@pytest.mark.parametrize("kind", ["symlink", "directory"])
@pytest.mark.parametrize("measure", [False, True])
def test_run_files_refuse_unsafe_types(
    host: Path, suffix: str, kind: str, measure: bool
) -> None:
    path = host / "state" / files.DIRECTORY / ("sample" + suffix)
    if path.exists():
        path.rename(host / "saved")
    if kind == "symlink":
        path.symlink_to(host / "missing")
    else:
        path.mkdir()
    result = execute(host, measure=measure)
    assert result.returncode == (73 if suffix == files.OWNER else 65)
    assert result.stdout == ""


@pytest.mark.parametrize("owner", ["other", "missing", "symlink"])
@pytest.mark.parametrize("measure", [False, True])
def test_owner_refusal_precedes_effects(host: Path, owner: str, measure: bool) -> None:
    path = host / "state" / files.DIRECTORY / "sample.owner"
    path.rename(host / "saved")
    if owner == "other":
        path.write_text("another coordinator")
    elif owner == "symlink":
        path.symlink_to(host / "saved")
    result = execute(host, measure=measure)
    assert result.returncode == 73
    assert result.stdout == ""
    assert not (host / "sockets").exists()


@pytest.mark.parametrize("component", range(6))
@pytest.mark.parametrize("kind", ["symlink", "file"])
@pytest.mark.parametrize("measure", [False, True])
def test_clone_components_refuse_links_and_files(
    host: Path, component: int, kind: str, measure: bool
) -> None:
    tree = tree_path(host)
    tree.mkdir(parents=True)
    path = tree.parents[4 - component] if component < 5 else tree
    path.rename(host / "saved")
    if kind == "symlink":
        path.symlink_to(host / "saved", target_is_directory=True)
    else:
        path.write_text("not a directory")
    result = execute(host, measure=measure)
    assert result.returncode == 65
    assert result.stdout == ""


@pytest.mark.parametrize("measure", [False, True])
def test_script_contract(host: Path, measure: bool) -> None:
    script = prune_remote.facts(
        "example-org/example-repo",
        "sample",
        str(host / "sockets"),
        owner="coordinator",
        measure=measure,
    )
    assert script.startswith(
        remote.owned("sample", "coordinator")
        + remote.socket_root(str(host / "sockets"))
    )
    assert "git " not in script
    assert ("follow => 0, no_chdir => 1" in script) is measure
    assert ("File::Find" in script) is measure
    assert script.endswith('"$d/sample.owner"\n')


def test_unmeasured_does_not_visit_worktree(host: Path) -> None:
    state = host / "state" / files.DIRECTORY
    (state / "sample.rc").write_text("not a return code")
    (state / "sample.keep").touch()
    os.utime(state / "sample.rc", (1234567890, 1234567890))
    tree = tree_path(host)
    bottom = tree.joinpath(*(["nested"] * 50))
    bottom.mkdir(parents=True)
    (bottom / "marker").write_text("never visit")
    os.mkfifo(tree / "pipe")
    unreadable = tree / "unreadable"
    unreadable.mkdir()
    (unreadable / "marker").touch()
    unreadable.chmod(0)
    try:
        result = execute(host, measure=False)
    finally:
        unreadable.chmod(0o700)
    assert result.returncode == 0, result.stderr
    assert result.stdout == "0\n1234567890\n1\n-\n-\n"
    assert result.stderr == ""
    # The only explicit stat in the Perl program reads the rc mtime.
    assert prune_remote.UNMEASURED.count("lstat(") == 1
    assert "lstat($rc)" in prune_remote.UNMEASURED
    assert "[7]" not in prune_remote.UNMEASURED
    assert "find" not in prune_remote.UNMEASURED

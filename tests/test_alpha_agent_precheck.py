"""Clone checks use plain directories and a local stand-in for SSH."""

from __future__ import annotations

import os
import subprocess
import sys
from typing import TYPE_CHECKING

import pytest

from tbhbdev.alpha import agent
lazy from tbhbdev.alpha.agent import precheck

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path


@pytest.fixture(autouse=True)
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    for key in ("XDG_CACHE_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME"):
        monkeypatch.setenv(key, str(tmp_path / key))
    for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR"):
        monkeypatch.delenv(key, raising=False)


@pytest.fixture
def clone(tmp_path: Path) -> Callable[[str, dict[str, bytes]], Path]:
    def build(name: str, hooks: dict[str, bytes]) -> Path:
        path = tmp_path / name
        directory = path / ".git" / "hooks"
        directory.mkdir(parents=True)
        for filename, content in hooks.items():
            (directory / filename).write_bytes(content)
        (directory.parent / "config").write_text("[core]\n bare = false\n")
        return path

    return build


@pytest.fixture
def ssh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    directory = tmp_path / "bin"
    directory.mkdir()
    executable = directory / "ssh"
    executable.write_text(
        '#!/bin/bash\nprintf "%s\\n" "$@" > "$HOME/ssh-args"\nshift\nexec "$@"\n'
    )
    executable.chmod(0o700)
    monkeypatch.setenv("PATH", f"{directory}:/usr/bin:/bin")
    return executable


@pytest.mark.usefixtures("ssh")
@pytest.mark.parametrize("remote", [False, True])
@pytest.mark.parametrize("prefix", ["", "~/"])
def test_command_collects_at_home(
    clone: Callable[[str, dict[str, bytes]], Path],
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    remote: bool,
    prefix: str,
) -> None:
    reference = clone("reference", {"pre-commit": b"hook\xff\n"})
    target = clone("clone", {"pre-commit": b"hook\xff\n", "unused.sample": b"other"})
    args = ["precheck", f"{prefix}clone", "--reference", f"{prefix}reference"]
    if remote:
        args += ["--host", "example-host"]
    assert agent.main(args, "example agent") == 0
    assert capsys.readouterr() == (f"checked: {target}/.git/config \nPRECHECK OK\n", "")
    assert reference.exists()
    if remote:
        assert (
            tmp_path / "ssh-args"
        ).read_text() == "example-host\nbash\n-s\n--\nclone\n"
    else:
        assert not (tmp_path / "ssh-args").exists()


@pytest.mark.parametrize("hooks", [{}, {"a.sample": b"ignored"}])
def test_empty_hook_sets(
    clone: Callable[[str, dict[str, bytes]], Path], hooks: dict[str, bytes]
) -> None:
    reference = clone("reference", hooks)
    target = clone("clone", hooks)
    result = precheck.precheck(precheck.collect(str(target)), reference)
    assert result.exit_code == 0
    assert result.lines == (f"checked: {target}/.git/config ", "PRECHECK OK")


@pytest.mark.parametrize(
    "line",
    [
        b'PREK="/home/example/.cache/prek/0.5.0/prek-aarch64-linux/prek"',
        b'PREK="/other/home/.cache/prek/0.6.0/prek-x86_64-linux/prek"',
    ],
)
def test_prek_normalization(
    clone: Callable[[str, dict[str, bytes]], Path], line: bytes
) -> None:
    baseline = b'PREK="/home/example/.cache/prek/0.4.0/prek-aarch64-linux/prek"'
    reference = clone(
        "reference", {"pre-commit": b"#!/bin/sh\n" + baseline + b"\nrun\n"}
    )
    target = clone("clone", {"pre-commit": b"#!/bin/sh\n" + line + b"\nrun\n"})
    assert precheck.precheck(precheck.collect(str(target)), reference).exit_code == 0


@pytest.mark.parametrize(
    ("before", "after"),
    [
        (b"a\xff", b"a\xfe"),
        (b"a\n", b"a"),
        (b"a\r\n", b"a\n"),
        (
            b'PREK="/x/prek/1/prek-linux/prek"\nrun',
            b'PREK="/x/prek/2/prek-linux/prek"\nother',
        ),
        (b' PREK="/x/prek/1/prek-linux/prek"', b' PREK="/x/prek/2/prek-linux/prek"'),
        (
            b'PREK="/x/prek/1/prek-linux/prek" tail',
            b'PREK="/x/prek/2/prek-linux/prek" tail',
        ),
        (b'PREK="/x/other/1/prek-linux/prek"', b'PREK="/x/other/2/prek-linux/prek"'),
    ],
)
def test_changed_hook_bytes_fail(
    clone: Callable[[str, dict[str, bytes]], Path], before: bytes, after: bytes
) -> None:
    reference = clone("reference", {"pre-commit": before})
    target = clone("clone", {"pre-commit": after})
    result = precheck.precheck(precheck.collect(str(target)), reference)
    assert result.exit_code == 1
    assert result.lines == (
        "MISMATCH hook pre-commit differs from the reference beyond the prek version",
        f"checked: {target}/.git/config ",
        "PRECHECK FAIL",
    )


def test_every_mismatch_is_reported_in_order(
    clone: Callable[[str, dict[str, bytes]], Path], capsys: pytest.CaptureFixture[str]
) -> None:
    reference = clone("reference", {"a": b"same", "b": b"before", "missing": b"x"})
    target = clone("clone", {"a": b"same", "b": b"after", "extra": b"x"})
    gitdir = target / ".git"
    (gitdir / "config").write_text("safe\n hooksPath = somewhere\n fsmonitor = yes\n")
    (gitdir / "config.worktree").write_text("[alias]\n")
    nested = gitdir / "worktrees" / "linked"
    nested.mkdir(parents=True)
    (nested / "config.extra").write_text("[includeIf something]\n")
    files = [gitdir / "config", gitdir / "config.worktree", nested / "config.extra"]
    assert precheck.main([str(target), "--reference", str(reference)], "check") == 1
    assert capsys.readouterr() == (
        (
            f"MISMATCH hook set: reference [a b missing] vs {target} [a b extra]\n"
            "MISMATCH hook b differs from the reference beyond the prek version\n"
            f"MISMATCH risky config: {files[0]}:2: hooksPath = somewhere\n"
            f"MISMATCH risky config: {files[0]}:3: fsmonitor = yes\n"
            f"MISMATCH risky config: {files[1]}:1:[alias]\n"
            f"MISMATCH risky config: {files[2]}:1:[includeIf something]\n"
            f"checked: {' '.join(map(str, files))} \nPRECHECK FAIL\n"
        ),
        "",
    )


@pytest.mark.parametrize(
    "risky",
    [
        "hooksPath",
        "FsMonitor",
        "SSHCommand",
        "[credential",
        "[alias",
        "[filter",
        "[diff",
        "[include",
        "[includeif",
        "[url",
    ],
)
def test_risky_pattern(
    clone: Callable[[str, dict[str, bytes]], Path], risky: str
) -> None:
    assert precheck.RISKY_PATTERN == (
        r"hookspath|fsmonitor|sshcommand|^[[:space:]]*\["
        r"(credential|alias|filter|diff|include|includeif|url)"
    )
    reference = clone("reference", {})
    target = clone("clone", {})
    config = target / ".git" / "config"
    config.write_text(f"safe\n\t{risky}\n")
    result = precheck.precheck(precheck.collect(str(target)), reference)
    assert result.exit_code == 1
    assert result.lines[0] == f"MISMATCH risky config: {config}:2:\t{risky}"


def test_no_configs_and_hidden_files_are_ignored(
    clone: Callable[[str, dict[str, bytes]], Path], tmp_path: Path
) -> None:
    reference = clone("reference", {".hidden": b"before"})
    target = clone("clone", {".hidden": b"after"})
    (target / ".git" / "config").rename(tmp_path / "saved")
    result = precheck.precheck(precheck.collect(str(target)), reference)
    assert result.lines == ("checked: ", "PRECHECK OK")
    assert result.exit_code == 0


@pytest.mark.usefixtures("ssh")
@pytest.mark.parametrize("remote", [False, True])
def test_missing_clone(
    clone: Callable[[str, dict[str, bytes]], Path],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    remote: bool,
) -> None:
    reference = clone("reference", {})
    target = tmp_path / "absent"
    (target / ".git").mkdir(parents=True)
    args = [str(target), "--reference", str(reference)]
    if remote:
        args += ["--host", "example-host"]
    assert precheck.main(args, "check") == 1
    assert capsys.readouterr() == (f"no clone at {target}/.git\nPRECHECK FAIL\n", "")


def test_missing_reference(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert precheck.main(["clone", "--reference", "absent"], "check") == 2
    assert capsys.readouterr() == (
        "",
        f"no reference clone at {tmp_path}/absent/.git\n",
    )


@pytest.mark.parametrize("args", [[], ["clone"], ["clone", "--reference"], ["--bad"]])
def test_usage_errors(args: list[str], capsys: pytest.CaptureFixture[str]) -> None:
    assert precheck.main(args, "example check") == 2
    output = capsys.readouterr()
    assert output.out == ""
    assert output.err.startswith("usage: example check ")


@pytest.mark.parametrize("body", ["exit 42", "cat >/dev/null; echo partial"])
def test_ssh_errors(
    clone: Callable[[str, dict[str, bytes]], Path],
    ssh: Path,
    capsys: pytest.CaptureFixture[str],
    body: str,
) -> None:
    reference = clone("reference", {})
    ssh.write_text(f"#!/bin/bash\n{body}\n")
    assert (
        precheck.main(
            ["clone", "--reference", str(reference), "--host", "example-host"], "check"
        )
        == 2
    )
    message = (
        "ssh to example-host failed"
        if body == "exit 42"
        else "truncated output from example-host"
    )
    assert capsys.readouterr() == ("", f"{message}\n")


def test_ssh_not_found(
    clone: Callable[[str, dict[str, bytes]], Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    reference = clone("reference", {})
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    assert (
        precheck.main(
            ["clone", "--reference", str(reference), "--host", "example-host"], "check"
        )
        == 2
    )
    assert capsys.readouterr() == ("", "ssh to example-host failed\n")


@pytest.mark.parametrize(
    "change", ["same", "different", "set", "risky", "missing", "empty", "sample"]
)
def test_reference_script_parity(
    clone: Callable[[str, dict[str, bytes]], Path],
    ssh: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    change: str,
) -> None:
    script = os.environ.get("TBHBDEV_PRECHECK_SCRIPT")
    if not script:
        pytest.skip("TBHBDEV_PRECHECK_SCRIPT is unset")
    reference = clone("reference/clone", {"pre-commit": b"hook\n"})
    target = clone("clone", {"pre-commit": b"hook\n"})
    if change == "different":
        (target / ".git/hooks/pre-commit").write_bytes(b"changed\n")
    elif change == "set":
        (target / ".git/hooks/extra").write_bytes(b"extra")
    elif change == "risky":
        (target / ".git/config").write_text("[alias]\n")
    elif change == "missing":
        (target / ".git/hooks").rename(tmp_path / "saved-hooks")
    elif change in {"empty", "sample"}:
        for path in (reference, target):
            (path / ".git/hooks/pre-commit").rename(path / ".git/saved-hook")
            if change == "sample":
                (path / ".git/hooks/unused.sample").write_bytes(b"sample")
    # The script derives the reference from HOME; SSH supplies a separate HOME.
    ssh.write_text(f'#!/bin/bash\nshift\nHOME={str(tmp_path)!r} exec "$@"\n')
    environment = dict(os.environ, HOME=str(reference.parent))
    original = subprocess.run(
        ["bash", script, "example-host", "clone"],
        env=environment,
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    status = precheck.main(
        [str(target), "--reference", str(reference), "--host", "example-host"], "check"
    )
    output = capsys.readouterr()
    assert output.err == original.stderr == ""
    assert (status, output.out.splitlines()[-1]) == (
        original.returncode,
        original.stdout.splitlines()[-1],
    )


def test_library_refuses_missing_reference(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match=f"^no reference clone at {tmp_path}/.git$"):
        precheck.precheck(precheck.CloneData("clone", {}, (), ()), tmp_path)


def test_help(capsys: pytest.CaptureFixture[str]) -> None:
    assert precheck.main(["--help"], "example check") == 0
    assert capsys.readouterr() == (
        (
            "usage: example check [-h] --reference REF [--host HOST] CLONE\n"
            "\nRead a clone's hooks and config without invoking Git.\n"
            "\npositional arguments:\n  CLONE\n"
            "\noptions:\n  -h, --help       show this help message and exit\n"
            "  --reference REF\n  --host HOST\n"
        ),
        "",
    )


@pytest.mark.parametrize("output", ["@@ENDsuffix\n", "@@END\r\n", "prefix@@END\n"])
def test_truncated_marker_must_be_a_whole_line(output: str) -> None:
    with pytest.raises(ValueError, match=r"^truncated output from example-host$"):
        precheck.parse_collection(output, "example-host")


def test_collection_preserves_config_bytes() -> None:
    data = precheck.parse_collection(
        "noise\n@@FILES\n/a\n/b\n@@RISKY\n/a:1:fsmonitor\r\n/b:1:\x0b[alias]\n@@END\n",
        "example-host",
    )
    assert data == precheck.CloneData(
        "example-host", {}, ("/a", "/b"), ("/a:1:fsmonitor\r", "/b:1:\x0b[alias]")
    )


def test_collection_errors_precede_truncation() -> None:
    data = precheck.parse_collection("@@ERR first\n@@ERR second\n", "example-host")
    assert data == precheck.CloneData("example-host", {}, (), (), "first\nsecond")


@pytest.mark.parametrize(
    "content",
    [
        b'PREK="/x\n/prek/1/prek-linux/prek"',
        b'PREK="/x/prek/1\n/prek-linux/prek"',
        b'PREK="/x/prek/1/prek-linux\n/prek"',
        b'PREK="/x/prek/1/prek-linux/prek"\r',
        b'PREK="/x/prek//prek-linux/prek"',
        b'PREK="/x/prek/1/prek-/prek"',
    ],
)
def test_only_a_complete_single_line_stub_is_normalized(content: bytes) -> None:
    assert precheck.normalize(content) == content


def test_dispatch_preserves_arguments_and_program(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[list[str], str]] = []

    def check(argv: list[str], prog: str) -> int:
        calls.append((argv, prog))
        return 37

    monkeypatch.setattr(precheck, "main", check)
    assert agent.main(["precheck", "a", "--reference", "b"], "example agent") == 37
    assert calls == [(["a", "--reference", "b"], "example agent precheck")]


@pytest.mark.skipif(
    sys.platform == "darwin",
    reason=(
        "the macOS filesystem refuses non-UTF-8 names (errno 92); "
        "Linux CI runs this test"
    ),
)
def test_collection_decodes_non_utf8_paths(
    clone: Callable[[str, dict[str, bytes]], Path],
) -> None:
    target = clone(os.fsdecode(b"clone\xff"), {})
    assert precheck.collect(str(target)).files == (str(target / ".git" / "config"),)


def test_normalization_preserves_surrounding_bytes() -> None:
    content = b'prefix\xff\nPREK="/home/example/prek/1/prek-linux/prek"\nsuffix\n'
    assert precheck.normalize(content) == b'prefix\xff\nPREK="<prek>"\nsuffix\n'


@pytest.mark.usefixtures("ssh")
def test_host_option_always_uses_ssh(
    clone: Callable[[str, dict[str, bytes]], Path], tmp_path: Path
) -> None:
    clone("clone", {})
    assert precheck.collect("clone", "").label == ""
    assert (tmp_path / "ssh-args").read_text() == "\nbash\n-s\n--\nclone\n"


def test_ssh_diagnostics_are_preserved(
    clone: Callable[[str, dict[str, bytes]], Path],
    ssh: Path,
    capfd: pytest.CaptureFixture[str],
) -> None:
    reference = clone("reference", {})
    ssh.write_text("#!/bin/bash\necho transport-error >&2\nexit 42\n")
    assert (
        precheck.main(
            ["clone", "--reference", str(reference), "--host", "example-host"], "check"
        )
        == 2
    )
    assert capfd.readouterr() == ("", "transport-error\nssh to example-host failed\n")


def test_library_resolves_reference_at_home(
    clone: Callable[[str, dict[str, bytes]], Path],
) -> None:
    reference = clone("reference", {})
    target = clone("clone", {})
    relative = reference.relative_to(reference.parent)
    assert precheck.precheck(precheck.collect(str(target)), relative).exit_code == 0

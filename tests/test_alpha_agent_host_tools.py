"""Host paths and script interpreters are resolved before execution."""

from __future__ import annotations

import dataclasses
import grp
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest
from agent_example import settings
from agent_host_example import bounded_file_reads, trusted_host_stat

from tbhbdev.alpha.agent import host_tools
from tbhbdev.alpha.agent.model import Invalid, Os


@pytest.fixture(autouse=True)
def host_stat(monkeypatch: pytest.MonkeyPatch) -> dict[Path, tuple[int, int]]:
    return trusted_host_stat(monkeypatch)


def script(path: Path, text: str) -> str:
    path.write_text(text)
    path.chmod(0o700)
    return str(path)


@pytest.mark.parametrize("relative", [False, True])
def test_executable_links_follow_directory_aliases_after_parent_components(
    tmp_path: Path, relative: bool
) -> None:
    release = tmp_path / "release"
    (release / "bin").mkdir(parents=True)
    script(release / "bin/codex", "binary")
    directory = tmp_path / "bin"
    directory.mkdir()
    current = tmp_path / "current"
    current.symlink_to(
        release.relative_to(tmp_path) if relative else release, target_is_directory=True
    )
    alias = directory / "codex"
    alias.symlink_to("../current/bin/codex" if relative else current / "bin/codex")
    assert host_tools.executable_links(alias) == (str(alias), str(current))


def test_executable_link_walk_refuses_missing_paths(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        host_tools.executable_links(tmp_path / "missing")


@pytest.mark.parametrize("os", Os)
def test_host_path_is_fixed_and_scoped(
    os: Os, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    original = settings()
    configured = dataclasses.replace(
        original,
        sandbox=dataclasses.replace(original.sandbox, host_path=("/example/bin",)),
    )
    monkeypatch.setenv("PATH", "/untrusted")
    entries = ("/usr/bin", "/bin", "/usr/sbin", "/sbin")
    if os is Os.MACOS:
        entries += ("/opt/homebrew/bin", "/usr/local/bin")
    assert host_tools.search_path(configured, os) == (*entries, "/example/bin")
    executable = script(tmp_path / "git", "binary")

    def which(name: str, *, path: str) -> str:
        assert name == "git"
        assert path == ":".join((*entries, "/example/bin"))
        return executable

    monkeypatch.setattr(host_tools.shutil, "which", which)
    before = host_tools.search()
    with host_tools.configured(configured, os):
        assert host_tools.search() == ":".join((*entries, "/example/bin"))
        assert Path(host_tools.resolve("git")).is_absolute()
    assert host_tools.search() == before


def test_resolution_follows_symlinks_and_refuses_missing(tmp_path: Path) -> None:
    target = script(tmp_path / "target", "#!/bin/sh\nexit 0\n")
    (tmp_path / "tool").symlink_to(target)
    assert host_tools.resolve("tool", str(tmp_path)) == target
    assert host_tools.resolve("tool", str(tmp_path), follow_symlinks=False) == str(
        tmp_path / "tool"
    )
    with pytest.raises(Invalid, match="host executable missing was not found"):
        host_tools.resolve("missing", str(tmp_path))


@pytest.mark.parametrize("indirect", [False, True])
def test_parent_commands_refuse_mise_shims(tmp_path: Path, indirect: bool) -> None:
    directory = tmp_path / "mise" / "shims"
    directory.mkdir(parents=True)
    target = script(tmp_path / "mise-binary", "binary")
    shim = directory / "codex"
    shim.symlink_to(target)
    search = directory
    if indirect:
        search = tmp_path / "bin"
        search.mkdir()
        (search / "codex").symlink_to(shim)
    with pytest.raises(Invalid) as caught:
        host_tools.resolve("codex", str(search))
    assert str(caught.value) == (
        "host executable codex is a mise shim; configure its fixed install directory"
    )


def test_mise_installation_protection_follows_path_aliases(tmp_path: Path) -> None:
    directory = tmp_path / "mise" / "shims"
    directory.mkdir(parents=True)
    alias = tmp_path / "alias"
    alias.symlink_to(directory, target_is_directory=True)
    assert host_tools.mise_installations((str(alias), str(directory))) == (
        str(directory.parent / "installs"),
    )


@pytest.mark.parametrize("relative", ["mise/bin", "other/shims"])
def test_fixed_install_directories_are_not_mise_shims(
    tmp_path: Path, relative: str
) -> None:
    directory = tmp_path / relative
    directory.mkdir(parents=True)
    executable = script(directory / "codex", "binary")
    assert host_tools.resolve("codex", str(directory)) == executable
    assert host_tools.mise_installations((str(directory),)) == ()


@pytest.mark.parametrize("env", [None, "", "-S "])
def test_script_interpreter_paths(tmp_path: Path, env: str | None) -> None:
    interpreter = script(tmp_path / "sh", "binary")
    launcher = script(tmp_path / "env", "binary")
    header = interpreter if env is None else f"{launcher} {env}sh"
    executable = script(tmp_path / "tool", f"#!{header}\nexit '0'\n: second-line\n")
    found = host_tools.executable_paths(
        executable, str(tmp_path), uid=1000, write_roots=()
    )
    assert found == (
        (executable, interpreter)
        if env is None
        else (executable, launcher, interpreter)
    )


@pytest.mark.parametrize(
    "header", ["sh", "", "{env}", "{env} -i sh", "{env} NAME=value sh"]
)
def test_invalid_script_interpreter(tmp_path: Path, header: str) -> None:
    launcher = script(tmp_path / "env", "binary")
    executable = script(tmp_path / "tool", "#!" + header.format(env=launcher) + "\n")
    with pytest.raises(Invalid) as caught:
        host_tools.executable_paths(executable, str(tmp_path), uid=1000, write_roots=())
    reason = (
        "no absolute interpreter"
        if header in ("sh", "")
        else "an unsupported env interpreter"
    )
    assert str(caught.value) == f"host script {executable} has {reason}"


def test_interpreter_cycle(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "tool"
    executable = script(path, f"#!{path}\n")
    original = host_tools.executable_header
    reads: list[str] = []

    def once(current: str) -> bytes:
        assert not reads, "a cyclic script must not be opened again"
        reads.append(current)
        return original(current)

    monkeypatch.setattr(host_tools, "executable_header", once)
    with pytest.raises(Invalid, match="interpreter cycle"):
        host_tools.executable_paths(executable, str(tmp_path), uid=1000, write_roots=())
    assert reads == [executable]


@pytest.mark.parametrize(
    ("listing", "version", "supported"),
    [
        ("network_proxy experimental false\n", None, True),
        ("other stable true\n", None, False),
        (None, "codex-cli 0.164.0\n", True),
        (None, "codex-cli 0.166.0", True),
        (None, "codex-cli 0.163.9", False),
        (None, "unexpected", False),
    ],
)
def test_codex_feature_check(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    listing: str | None,
    version: str | None,
    supported: bool,
) -> None:
    calls = []

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        assert command == ["/example/codex", "features", "list"]
        assert kwargs == {
            "env": {
                "HOME": str(tmp_path),
                "CODEX_HOME": str(tmp_path),
                "PATH": host_tools.search(),
            },
            "cwd": tmp_path,
            "text": True,
            "capture_output": True,
            "check": False,
            "timeout": 10,
        }
        calls.append("features")
        return subprocess.CompletedProcess(
            command,
            1 if listing is None else 0,
            listing or "",
            "error: unrecognized subcommand 'features'" if listing is None else "",
        )

    def output(command: list[str], **kwargs: object) -> str:
        assert command == ["/example/codex", "--version"]
        assert kwargs == {
            "env": {
                "HOME": str(tmp_path),
                "CODEX_HOME": str(tmp_path),
                "PATH": host_tools.search(),
            },
            "cwd": tmp_path,
            "text": True,
            "stderr": subprocess.DEVNULL,
            "timeout": 10,
        }
        calls.append("version")
        assert version is not None
        return version

    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(subprocess, "check_output", output)
    assert host_tools.codex_proxy_supported("/example/codex", tmp_path) is supported
    assert calls == (["features", "version"] if listing is None else ["features"])


@pytest.mark.parametrize("error", [OSError(), subprocess.TimeoutExpired("codex", 10)])
def test_feature_check_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    def fail(*_args: object, **_kwargs: object) -> None:
        raise error

    monkeypatch.setattr(subprocess, "run", fail)
    assert host_tools.codex_proxy_supported("/example/codex", tmp_path) is False


def test_nonregular_executable_cannot_block_inspection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:

    path = tmp_path / "pipe"
    os.mkfifo(path, 0o700)
    original = os.open

    def opened(name: str, flags: int) -> int:
        assert flags == os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
        return original(name, flags)

    monkeypatch.setattr(os, "open", opened)
    with pytest.raises(Invalid, match="not a regular file"):
        host_tools.executable_paths(str(path), str(tmp_path), uid=1000, write_roots=())


def test_installation_resolves_all_runtime_locations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "installed"
    root.mkdir()
    linked = tmp_path / "alias"
    linked.symlink_to(root, target_is_directory=True)
    monkeypatch.setattr(sys, "prefix", str(linked / "prefix"))
    monkeypatch.setattr(sys, "base_prefix", str(linked / "base"))
    monkeypatch.setattr(sys, "executable", str(linked / "python"))
    monkeypatch.setattr(sys, "argv", [str(linked / "tbhbdev")])
    monkeypatch.setattr(sys, "path", [str(linked / "library"), ""])
    assert host_tools.installation() == (
        str(root / "prefix"),
        str(root / "base"),
        str(root),
        str(root),
        str(root / "library"),
        str(Path.cwd()),
    )


def test_executable_reads_are_bounded_and_nofollow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = script(tmp_path / "binary", "binary data")
    opened = os.open

    def open_checked(path: str, flags: int) -> int:
        assert flags == os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
        return opened(path, flags)

    monkeypatch.setattr(os, "open", open_checked)
    bounded_file_reads(monkeypatch, 4096)
    assert host_tools.executable_paths(
        executable, str(tmp_path), uid=1000, write_roots=()
    ) == (executable,)


def test_env_interpreter_uses_the_policy_search_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    launcher = script(tmp_path / "env", "binary")
    executable = script(tmp_path / "tool", f"#!{launcher} selected\nexit 0\n")
    interpreter = script(tmp_path / "selected", "binary")
    monkeypatch.setenv("PATH", "/untrusted")
    original = host_tools.resolve

    def resolve(
        name: str, path: str | None = None, *, follow_symlinks: bool = True
    ) -> str:
        assert follow_symlinks is False
        return original(name, path, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(host_tools, "resolve", resolve)
    assert host_tools.executable_paths(
        executable, str(tmp_path), uid=1000, write_roots=()
    ) == (
        executable,
        launcher,
        interpreter,
    )


def test_feature_listing_errors_cannot_fall_back_to_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 1, "", "configuration error")

    def version(*_args: object, **_kwargs: object) -> str:
        pytest.fail("a failed feature check must refuse")

    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(subprocess, "check_output", version)
    assert host_tools.codex_proxy_supported("/example/codex", tmp_path) is False


def test_symlinked_host_binary(tmp_path: Path) -> None:
    target = script(tmp_path / "multicall", "binary")
    alias = tmp_path / "true"
    alias.symlink_to(target)
    assert host_tools.executable_paths(
        str(alias), str(tmp_path), uid=1000, write_roots=()
    ) == (str(alias), target)


@pytest.mark.parametrize("platform", ["darwin", "linux"])
@pytest.mark.parametrize("env", [False, True])
def test_root_owned_multicall_and_interpreter_chains(
    tmp_path: Path,
    env: bool,
    platform: str,
    monkeypatch: pytest.MonkeyPatch,
    host_stat: dict[Path, tuple[int, int]],
) -> None:
    monkeypatch.setattr(sys, "platform", platform)
    directory = tmp_path / "bin"
    directory.mkdir()
    multicall = script(directory / "multicall", "binary")
    true = directory / "true"
    launcher = directory / "env"
    true.symlink_to("multicall")
    launcher.symlink_to("multicall")
    selected = directory / "selected"
    hop = directory / "interpreter"
    selected.symlink_to(hop.name)
    hop.symlink_to("true")
    tool = script(
        directory / "tool",
        f"#!{launcher} -S selected\n" if env else f"#!{selected}\n",
    )
    alias = tmp_path / "alias"
    alias.symlink_to(directory, target_is_directory=True)
    executable = str(alias / "tool")
    mode = 0o777 if platform == "linux" else 0o755
    for link in (true, launcher, selected, hop, alias):
        host_stat[link] = (0, stat.S_IFLNK | mode)
    expected = [executable, str(alias), tool]
    if env:
        expected.extend((str(launcher), multicall))
    expected.extend((str(selected), str(hop), str(true), multicall))
    assert host_tools.executable_paths(
        executable, str(directory), uid=1000, write_roots=()
    ) == tuple(dict.fromkeys(expected))
    assert host_tools.executable_paths(
        str(true), str(directory), uid=1000, write_roots=()
    ) == (str(true), multicall)


@pytest.mark.parametrize("component", ["link", "directory", "file"])
@pytest.mark.parametrize("owner", [0, 1000, 2000])
@pytest.mark.parametrize("writable", [0, 0o020, 0o002])
def test_chain_component_trust(
    tmp_path: Path,
    host_stat: dict[Path, tuple[int, int]],
    component: str,
    owner: int,
    writable: int,
) -> None:
    directory = tmp_path / "install"
    directory.mkdir()
    target = script(directory / "binary", "binary")
    alias = tmp_path / "tool"
    alias.symlink_to(target)
    path, kind = {
        "link": (alias, stat.S_IFLNK),
        "directory": (directory, stat.S_IFDIR),
        "file": (Path(target), stat.S_IFREG),
    }[component]
    host_stat[path] = (owner, kind | 0o755 | writable)
    if owner == 2000 or writable:
        with pytest.raises(Invalid) as caught:
            host_tools.executable_paths(
                str(alias), str(tmp_path), uid=1000, write_roots=()
            )
        assert str(caught.value) == f"untrusted host executable component: {path}"
    else:
        assert host_tools.executable_paths(
            str(alias), str(tmp_path), uid=1000, write_roots=()
        ) == (str(alias), target)


@pytest.mark.parametrize("component", ["link", "directory", "file"])
def test_account_component_inside_write_root_is_refused(
    tmp_path: Path, host_stat: dict[Path, tuple[int, int]], component: str
) -> None:
    directory = tmp_path / "install"
    directory.mkdir()
    binary = directory / "binary"
    script(binary, "binary")
    alias = tmp_path / "tool"
    alias.symlink_to(binary)
    path = {"link": alias, "directory": directory, "file": binary}[component]
    host_stat[path] = (1000, stat.S_IFLNK | 0o755 if path == alias else 0o755)
    root = binary if component == "link" else path
    with pytest.raises(Invalid) as caught:
        host_tools.executable_paths(
            str(alias), str(tmp_path), uid=1000, write_roots=(str(root),)
        )
    # The outside link is refused first because its resolved target is writable.
    assert str(caught.value) == f"untrusted host executable component: {alias}"


def test_link_target_write_root_is_resolved(
    tmp_path: Path,
) -> None:
    writable = tmp_path / "writable"
    writable.mkdir()
    script(writable / "binary", "binary")
    root = tmp_path / "root-alias"
    root.symlink_to(writable, target_is_directory=True)
    alias = tmp_path / "outside"
    alias.symlink_to("writable/binary")
    with pytest.raises(Invalid) as caught:
        host_tools.executable_paths(
            str(alias), str(tmp_path), uid=1000, write_roots=(str(root),)
        )
    assert str(caught.value) == f"untrusted host executable component: {alias}"


def test_target_parent_step_does_not_hide_untrusted_directory(
    tmp_path: Path, host_stat: dict[Path, tuple[int, int]]
) -> None:
    directory = tmp_path / "untrusted"
    directory.mkdir()
    target = script(tmp_path / "binary", "binary")
    alias = tmp_path / "tool"
    alias.symlink_to("untrusted/../binary")
    host_stat[directory] = (2000, stat.S_IFDIR | 0o755)
    with pytest.raises(Invalid) as caught:
        host_tools.executable_paths(str(alias), str(tmp_path), uid=1000, write_roots=())
    assert str(caught.value) == f"untrusted host executable component: {directory}"
    assert Path(target).exists()


@pytest.mark.parametrize("component", ["directory", "file"])
def test_account_component_in_write_root_without_alias(
    tmp_path: Path, host_stat: dict[Path, tuple[int, int]], component: str
) -> None:
    directory = tmp_path / "install"
    directory.mkdir()
    executable = script(directory / "tool", "binary")
    path = directory if component == "directory" else Path(executable)
    host_stat[path] = (1000, 0o755)
    with pytest.raises(Invalid) as caught:
        host_tools.executable_paths(
            executable, str(tmp_path), uid=1000, write_roots=(str(path),)
        )
    assert str(caught.value) == f"untrusted host executable component: {path}"


def test_preserved_lookup_makes_relative_search_absolute(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script(tmp_path / "target", "binary")
    (tmp_path / "tool").symlink_to("target")
    monkeypatch.chdir(tmp_path)
    assert host_tools.resolve("tool", ".", follow_symlinks=False) == str(
        tmp_path / "tool"
    )


@pytest.mark.parametrize("mode", [0o755, 0o775, 0o757])
def test_root_component_is_checked(
    tmp_path: Path, host_stat: dict[Path, tuple[int, int]], mode: int
) -> None:
    executable = script(tmp_path / "binary", "binary")
    host_stat[Path("/")] = (2000 if mode == 0o755 else 0, stat.S_IFDIR | mode)
    with pytest.raises(Invalid, match=r"^untrusted host executable component: /$"):
        host_tools.executable_paths(executable, str(tmp_path), uid=1000, write_roots=())


def test_root_owned_linux_symlink_with_fixed_mode_is_accepted(
    tmp_path: Path,
    host_stat: dict[Path, tuple[int, int]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    target = script(tmp_path / "multicall", "binary")
    alias = tmp_path / "true"
    alias.symlink_to("multicall")
    # Ordinary Linux links always report this mode, which cannot be changed.
    host_stat[alias] = (0, stat.S_IFLNK | 0o777)
    assert host_tools.executable_paths(
        str(alias), str(tmp_path), uid=1000, write_roots=()
    ) == (str(alias), target)


@pytest.mark.parametrize("platform", ["linux", "darwin"])
@pytest.mark.parametrize("kind", [stat.S_IFLNK, stat.S_IFDIR, stat.S_IFREG])
@pytest.mark.parametrize("mode", [0o755, 0o775, 0o757, 0o777])
def test_component_write_permissions_follow_kernel_semantics(
    monkeypatch: pytest.MonkeyPatch, platform: str, kind: int, mode: int
) -> None:
    monkeypatch.setattr(sys, "platform", platform)
    expected = mode != 0o755 and not (
        platform == "linux" and kind == stat.S_IFLNK and mode == 0o777
    )
    info = os.stat_result((kind | mode, 0, 0, 1, 0, 0, 0, 0, 0, 0))
    assert host_tools.writable_component(info) is expected


@pytest.mark.parametrize(
    "case",
    [
        ("darwin", 0, 80, "admin", 0o775),
        ("darwin", 0, 80, "admin", 0o777),
        ("darwin", 0, 81, "admin", 0o775),
        ("darwin", 0, 80, "staff", 0o775),
        ("darwin", 0, 80, KeyError, 0o775),
        ("darwin", 0, 80, OSError, 0o775),
        ("darwin", 1000, 80, "admin", 0o775),
        ("darwin", 2000, 80, "admin", 0o775),
        ("linux", 0, 80, "admin", 0o775),
    ],
)
def test_admin_directory_component_trust(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: tuple[str, int, int, str | type[Exception], int],
) -> None:
    platform, owner, group, lookup, mode = case
    monkeypatch.setattr(sys, "platform", platform)
    directory = tmp_path / "Applications"
    directory.mkdir()
    executable = script(directory / "binary", "binary")
    original = host_tools.lstat

    def info(path: Path) -> os.stat_result:
        values = list(original(path))
        if path == directory:
            values[0], values[4], values[5] = stat.S_IFDIR | mode, owner, group
        return os.stat_result(values)

    def getgrgid(gid: int) -> grp.struct_group:
        assert gid == 80
        if not isinstance(lookup, str):
            raise lookup
        return grp.struct_group((lookup, "x", gid, []))

    monkeypatch.setattr(host_tools, "lstat", info)
    monkeypatch.setattr(grp, "getgrgid", getgrgid)
    if case == ("darwin", 0, 80, "admin", 0o775):
        assert host_tools.executable_chain(executable, 1000, ()) == (executable,)
    else:
        with pytest.raises(Invalid) as caught:
            host_tools.executable_chain(executable, 1000, ())
        assert str(caught.value) == (
            f"untrusted host executable component: {directory}"
        )


def test_env_alias_keeps_every_hop_and_checks_its_selected_target(
    tmp_path: Path,
) -> None:
    target = script(tmp_path / "multicall", "binary")
    env = tmp_path / "env"
    env.symlink_to("multicall")
    launcher = tmp_path / "launcher"
    launcher.symlink_to("env")
    selected = tmp_path / "selected"
    selected.symlink_to("multicall")
    executable = script(tmp_path / "tool", f"#!{launcher} selected\n")
    assert host_tools.executable_paths(
        executable, str(tmp_path), uid=1000, write_roots=()
    ) == (executable, str(launcher), str(env), target, str(selected))


@pytest.mark.parametrize("env", [False, True])
def test_account_owned_interpreters_outside_write_roots_are_accepted(
    tmp_path: Path, host_stat: dict[Path, tuple[int, int]], env: bool
) -> None:
    selected = script(tmp_path / "selected", "binary")
    launcher = script(tmp_path / "env", "binary")
    for path in (selected, launcher):
        host_stat[Path(path)] = (1234, stat.S_IFREG | 0o755)
    header = f"{launcher} selected" if env else selected
    executable = script(tmp_path / "tool", f"#!{header}\n")
    assert host_tools.executable_paths(
        executable, str(tmp_path), uid=1234, write_roots=(str(tmp_path / "writable"),)
    ) == ((executable, launcher, selected) if env else (executable, selected))


@pytest.mark.parametrize("operating_system", Os)
@pytest.mark.parametrize("executable", ["/usr/bin/git", "/example/bin/git"])
def test_git_shim_only_applies_to_system_git_on_macos(
    monkeypatch: pytest.MonkeyPatch, operating_system: Os, executable: str
) -> None:
    if operating_system is Os.MACOS and executable == "/usr/bin/git":
        return

    def readlink(_path: str | os.PathLike[str]) -> str:
        pytest.fail("other git executables must not inspect the developer directory")

    monkeypatch.setattr(os, "readlink", readlink)
    assert host_tools.git_shim_paths(executable, operating_system, 1000, ()) == ()


def developer_git_state(
    case: str,
    selector: Path,
    developer: Path,
    fallback: Path,
    host_stat: dict[Path, tuple[int, int]],
) -> tuple[str, ...]:
    host_stat.update(
        {
            "account owned": {
                developer: (1000, stat.S_IFDIR | 0o755),
                developer / "usr/bin/git": (1000, stat.S_IFREG | 0o755),
            },
            "account link": {selector: (1000, stat.S_IFLNK | 0o755)},
            "write root": {developer: (1000, stat.S_IFDIR | 0o755)},
            "group writable": {developer: (0, stat.S_IFDIR | 0o775)},
            "other owner": {developer: (1001, stat.S_IFDIR | 0o755)},
            "link writable": {selector: (0, stat.S_IFLNK | 0o775)},
            "link owner": {selector: (1001, stat.S_IFLNK | 0o755)},
        }.get(case, {})
    )
    missing = {"missing git": developer, "missing fallback": fallback}.get(case)
    if missing is not None:
        (missing / "usr/bin/git").rename(missing / "usr/bin/moved-git")
    return {
        "git write root": (str(developer / "usr/bin/git"),),
        "write root": (str(developer),),
    }.get(case, ())


@pytest.mark.parametrize(
    "case",
    [
        "xcode",
        "account owned",
        "account link",
        "git write root",
        "fallback",
        "write root",
        "group writable",
        "other owner",
        "link writable",
        "link owner",
        "missing git",
        "missing fallback",
        "unreadable",
    ],
)
def test_git_shim_checks_selected_developer_chain(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    host_stat: dict[Path, tuple[int, int]],
    case: str,
) -> None:
    selector = tmp_path / "xcode_select_link"
    developer = tmp_path / "Xcode.app/Contents/Developer"
    fallback = tmp_path / "CommandLineTools"
    for directory in (developer, fallback):
        (directory / "usr/bin").mkdir(parents=True)
        script(directory / "usr/bin/git", "binary")
    selector.symlink_to(developer, target_is_directory=True)
    monkeypatch.setattr(host_tools, "_XCODE_SELECT_LINK", str(selector), raising=False)
    monkeypatch.setattr(host_tools, "_COMMAND_LINE_TOOLS", str(fallback), raising=False)
    readlink = os.readlink
    reads: list[str] = []

    def selected(path: str | os.PathLike[str], *, dir_fd: int | None = None) -> str:
        if str(path) == str(selector):
            reads.append(str(path))
            error = {
                "fallback": FileNotFoundError,
                "missing fallback": FileNotFoundError,
                "unreadable": PermissionError,
            }.get(case)
            if error is not None:
                raise error
        return readlink(path, dir_fd=dir_fd)

    monkeypatch.setattr(os, "readlink", selected)
    monkeypatch.setattr(host_tools.sys, "platform", "darwin")
    roots = developer_git_state(case, selector, developer, fallback, host_stat)
    if case in ("xcode", "account owned", "account link", "fallback"):
        result = host_tools.git_shim_paths("/usr/bin/git", Os.MACOS, 1000, roots)
        assert result == (
            (str(selector), str(developer), str(developer / "usr/bin/git"))
            if case != "fallback"
            else (str(fallback / "usr/bin/git"),)
        )
    else:
        with pytest.raises(Invalid) as caught:
            host_tools.git_shim_paths("/usr/bin/git", Os.MACOS, 1000, roots)
        component = {
            "git write root": developer / "usr/bin/git",
            "write root": selector,
            "link writable": selector,
            "link owner": selector,
        }.get(case, developer)
        assert str(caught.value) == (
            "cannot inspect the developer git executable"
            if case in ("missing git", "missing fallback", "unreadable")
            else f"untrusted host executable component: {component}"
        )
    assert reads
    assert all(path == str(selector) for path in reads)

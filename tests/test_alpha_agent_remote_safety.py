"""Run the host scripts in scratch directories, through the real shell."""

from __future__ import annotations

import os
import shlex
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest
from agent_driver_example import (
    commit_in,
    execution_arguments,
    git_in,
    mirror_in,
    write_standalone_fake,
)
from agent_example import LINUX, settings, trusted_executables

from tbhbdev.alpha import agent
from tbhbdev.alpha.agent import driver, transport
from tbhbdev.alpha.agent.decisions import policy, remote


@pytest.fixture(autouse=True)
def fixed_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    trusted_executables(monkeypatch)


@pytest.fixture
def host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    state = tmp_path / "state/tbhbdev/agent-runs"
    state.mkdir(parents=True)
    (state / "sample.owner").write_text("example-owner\n")
    tools = tmp_path / "bin"
    tools.mkdir()
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("PATH", str(tools) + ":/usr/bin:/bin")
    return tmp_path


def execute(script: str) -> subprocess.CompletedProcess[bytes]:
    script = script.replace(
        "PATH=/usr/bin:/bin:/usr/sbin:/sbin", "PATH=" + os.environ["PATH"]
    )
    script = script.replace(
        'PATH="$PATH:/opt/homebrew/bin:/usr/local/bin"', 'PATH="$PATH"'
    )
    script = script.replace(settings().sockets_dir, os.environ["HOME"] + "/sockets")
    return subprocess.run(["/bin/sh", "-c", script], capture_output=True, check=False)


def inspect_output(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    limit: int | None,
) -> str:
    async def run(args: tuple[str, ...], _data: bytes) -> tuple[int, bytes]:
        command = shlex.split(shlex.split(args[-1])[2])
        result = execute(command[2])
        return result.returncode, result.stdout

    monkeypatch.setattr(transport, "execute", run)
    args = agent.parser("example").parse_args(execution_arguments("example.md"))
    run = driver.prepare(settings(), args)
    run.owner = "example-owner"
    assert driver.inspect(settings(), run, limit=limit) == 0
    return capsys.readouterr().out


@pytest.mark.parametrize("error", ["unrelated failure", "missing"])
@pytest.mark.parametrize("os_name", ["Linux", "Darwin"])
def test_tmux_error_never_writes_rc(host: Path, error: str, os_name: str) -> None:
    write_standalone_fake(host / "bin/uname", f"print({os_name!r})\n")
    if error != "missing":
        write_standalone_fake(
            host / "bin/tmux",
            "import sys\nsys.stderr.write('unrelated failure')\nsys.exit(1)\n",
        )
    else:
        # A fixed PATH containing only scratch tools also tests a missing tmux.
        for name in ("mkdir", "chmod", "mv", "find", "id", "cat"):
            program = next(
                p
                for p in (
                    Path("/usr/bin") / name,
                    Path("/bin") / name,
                )
                if p.exists()
            )
            (host / "bin" / name).symlink_to(program)
        os.environ["PATH"] = str(host / "bin")
    result = execute(
        remote.fallback("sample", settings().sockets_dir, owner="example-owner")
    )
    assert result.returncode == 75
    assert not (host / "state/tbhbdev/agent-runs/sample.rc").exists()


def test_session_is_checked_before_rc(host: Path) -> None:
    write_standalone_fake(
        host / "bin/tmux",
        "import os, sys\nfrom pathlib import Path\n"
        "root = Path(os.environ['XDG_STATE_HOME'])\n"
        "(root / 'tbhbdev/agent-runs/sample.rc').write_text('0')\n"
        'sys.stderr.write("can\'t find session: ag-sample\\n")\nsys.exit(1)\n',
    )
    result = execute(
        remote.read("sample", settings().sockets_dir, owner="example-owner")
    )
    assert result.returncode == 0
    assert result.stdout.splitlines()[1] == b"30"


@pytest.mark.parametrize(
    "case",
    [
        (".phase", b"running\n0\n1\n1", False, None),
        (".rc", b"256", False, None),
        (".rc", b"x", False, None),
        (".out", b"answer\xff", True, "answer\ufffd"),
        (".log", b"\x1b[31m hello\x07", False, "[31m hello"),
        (".log", None, False, None),
    ],
)
def test_untrusted_run_files(
    host: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: tuple[str, bytes | None, bool, str | None],
) -> None:
    suffix, content, answer, expected = case
    write_standalone_fake(
        host / "bin/tmux",
        "import sys\n"
        'sys.stderr.write("can\'t find session: ag-sample\\n")\nsys.exit(1)\n',
    )
    path = host / "state/tbhbdev/agent-runs" / ("sample" + suffix)
    if content is None:
        target = host / "other-file"
        target.write_text("must never read this")
        path.symlink_to(target)
    else:
        path.write_bytes(content)

    async def run(args: tuple[str, ...], _data: bytes) -> tuple[int, bytes]:
        command = shlex.split(shlex.split(args[-1])[2])
        result = execute(command[2])
        return result.returncode, result.stdout

    monkeypatch.setattr(transport, "execute", run)
    if expected is None:
        with pytest.raises((ValueError, transport.TransportError)):
            transport.read(
                settings(),
                LINUX,
                ("sample", "example-owner"),
                answer=answer,
            )
    else:
        assert (
            transport.read(
                settings(),
                LINUX,
                ("sample", "example-owner"),
                answer=answer,
            ).text
            == expected
        )


@pytest.mark.parametrize("bad", ["file", "symlink", "mode", "socket-link"])
def test_socket_directory_refuses_unsafe_paths(host: Path, bad: str) -> None:
    directory = host / "sockets"
    if bad == "file":
        directory.write_text("file")
    elif bad == "symlink":
        directory.symlink_to(host, target_is_directory=True)
    else:
        directory.mkdir()
        directory.chmod(0o755 if bad == "mode" else 0o700)
        if bad == "socket-link":
            (directory / "agents").symlink_to(host / "elsewhere")
    assert (
        execute(
            remote.read("sample", settings().sockets_dir, owner="example-owner")
        ).returncode
        == 65
    )


@pytest.mark.parametrize("suffix", [".keep", ".rc.pending"])
def test_exclusive_markers_refuse_symlinks(host: Path, suffix: str) -> None:
    target = host / "other-file"
    target.write_text("preserve")
    path = host / "state/tbhbdev/agent-runs" / ("sample" + suffix)
    path.symlink_to(target)
    write_standalone_fake(
        host / "bin/tmux",
        'import sys\nsys.stderr.write("no server running on example")\nsys.exit(1)\n',
    )
    script = (
        remote.keep("sample", owner="example-owner")
        if suffix == ".keep"
        else remote.fallback("sample", settings().sockets_dir, owner="example-owner")
    )
    assert execute(script).returncode != 0
    assert target.read_text() == "preserve"
    assert path.is_symlink()


def test_scripts_have_fixed_path_socket_and_git_environment() -> None:
    config = settings()
    scripts = [
        remote.session(
            "sample", "kill-session", config.sockets_dir, owner="example-owner"
        ),
        remote.transfer("sample", config.sockets_dir, owner="example-owner"),
        remote.start("sample", config.sockets_dir, LINUX, owner="example-owner")[2],
        remote.read("sample", config.sockets_dir, owner="example-owner"),
        remote.read("sample", config.sockets_dir, answer=True, owner="example-owner"),
        remote.fallback("sample", config.sockets_dir, owner="example-owner"),
        remote.clean(
            "example-org/example-repo",
            "sample",
            "stamp",
            config.sockets_dir,
            decision=remote.Cleanup("example-owner", force=False),
        ),
    ]
    for script in scripts:
        assert script.startswith("PATH=/usr/bin:/bin:/usr/sbin:/sbin\nexport PATH\n")
        assert 'PATH="$PATH:/opt/homebrew/bin:/usr/local/bin"' in script
        assert 'tmux -S "$s/agents" -f /dev/null' in script
        assert "tmux -L" not in script
        assert f"s={config.sockets_dir}\n" in script
    assert " git " not in scripts[-1]
    assert "perl" not in scripts[-1]
    assert remote.installed(LINUX).endswith('"$HOME/.local/bin/tbhbdev" --version')
    assert (
        "-lc"
        not in remote.start("sample", config.sockets_dir, LINUX, owner="example-owner")[
            2
        ]
    )
    for script in [
        remote.origin_branch(
            "example-org/example-repo", "sample", owner="example-owner"
        ),
        remote.preflight("example-org/example-repo", "main", "example", LINUX),
    ]:
        for line in script.splitlines():
            if " git " in line:
                assert 'env -i PATH="$PATH" HOME="$HOME" LANG=C LC_ALL=C' in line
                assert (
                    "GIT_TERMINAL_PROMPT=0 GIT_NO_LAZY_FETCH=1 GIT_CONFIG_NOSYSTEM=1"
                    in line
                )
                assert "GIT_ASKPASS=/bin/false SSH_ASKPASS=/bin/false" in line
                assert "-c core.fsmonitor=false -c core.sshCommand=ssh" in line
                assert "-c credential.helper= -c core.askPass=" in line
                assert "-c protocol.ext.allow=never -c core.pager=cat" in line
                assert ('SSH_AUTH_SOCK="${SSH_AUTH_SOCK:-}"' in line) == (
                    "ls-remote" in line
                )
        assert "worktree remove" not in script


@pytest.mark.parametrize("path", [".private/sockets", "~/.private/sockets"])
def test_socket_home_relative_paths(host: Path, path: str) -> None:
    script = remote.socket_root(path) + 'printf "%s" "$s/agents"\n'
    result = execute(script)
    assert result.returncode == 0
    assert result.stdout.decode() == policy.resolve(str(host), path) + "/agents"


def test_start_command_and_absolute_program() -> None:
    command = remote.start("example", "/example/sockets", LINUX, owner="example-owner")
    assert command[:2] == ("/bin/sh", "-c")
    assert shlex.split(command[2].splitlines()[-1]) == shlex.split(
        "tmux -S '$s/agents' -f /dev/null new-session -d -s ag-example "
        "'$HOME/.local/bin/tbhbdev' alpha agent run-host example --host example-linux"
    )
    host = replace(LINUX, tbhbdev="/example path/bin/tbhbdev")
    assert remote.program(host) == "'/example path/bin/tbhbdev'"
    assert remote.installed(host).endswith("'/example path/bin/tbhbdev' --version")


def test_read_defaults_to_bounded_log(host: Path) -> None:
    state = host / "state/tbhbdev/agent-runs"
    (state / "sample.log").write_bytes(b"L" * 70000)
    (state / "sample.out").write_bytes(b"O" * 70000)
    write_standalone_fake(
        host / "bin/tmux",
        'import sys\nsys.stderr.write("no server running on example")\nsys.exit(1)\n',
    )
    result = execute(
        remote.read("sample", settings().sockets_dir, owner="example-owner")
    )
    assert result.returncode == 0
    assert result.stdout.split(b"\n", 4)[4] == b"L" * 65536
    result = execute(
        remote.read(
            "sample", settings().sockets_dir, answer=True, owner="example-owner"
        )
    )
    assert result.returncode == 0
    assert result.stdout.split(b"\n", 4)[4] == b"O" * 65536


def test_claim_refusal_is_distinct_even_for_redirection_failure(host: Path) -> None:
    # A normal directory collision is checked before the exclusive creation.
    # Isolate the creation step too: sh treats ':' as a special builtin.
    claim = host / "existing"
    claim.write_text("preserve")
    script = remote.transfer("sample", settings().sockets_dir, owner="example-owner")
    line = next(line for line in script.splitlines() if '> "$d/sample.owner"' in line)
    line = line.replace('"$d/sample.owner"', shlex.quote(str(claim)))
    result = execute("set -C\n" + line)
    assert result.returncode == 73
    assert claim.read_text() == "preserve"


@pytest.mark.parametrize("component", ["state", "tbhbdev", "agent-runs"])
def test_run_directory_symlinks_are_not_followed(host: Path, component: str) -> None:
    root = host / "state/tbhbdev/agent-runs"
    (root / "sample.log").write_text("private marker")
    path = next(p for p in (root, *root.parents) if p.name == component)
    path.rename(host / "private")
    path.symlink_to(host / "private", target_is_directory=True)
    write_standalone_fake(
        host / "bin/tmux",
        'import sys\nsys.stderr.write("no server running on example")\nsys.exit(1)\n',
    )
    result = execute(
        remote.read("sample", settings().sockets_dir, owner="example-owner")
    )
    assert result.returncode == 65
    assert b"private marker" not in result.stdout


def test_session_target_never_matches_another_runs_prefix(host: Path) -> None:
    marker = host / "other-session-stopped"
    write_standalone_fake(
        host / "bin/tmux",
        "import sys\nfrom pathlib import Path\n"
        "if sys.argv[-1] == '=ag-sample':\n"
        '    sys.stderr.write("can\'t find session: ag-sample")\n'
        "    sys.exit(1)\n"
        f"Path({str(marker)!r}).write_text('stopped ag-sample-other')\n",
    )
    result = execute(
        remote.session(
            "sample", "kill-session", settings().sockets_dir, owner="example-owner"
        )
    )
    assert result.returncode == 0
    assert not marker.exists()


def no_session(host: Path) -> None:
    write_standalone_fake(
        host / "bin/tmux",
        'import sys\nsys.stderr.write("no server running on example")\nsys.exit(1)\n',
    )


def clone_in(host: Path) -> Path:
    clone = host / "Code/github.com/example-org/example-repo"
    tree = clone / ".worktrees/agent-sample"
    (tree / ".git").mkdir(parents=True)
    (tree / "agent-file").write_text("preserve")
    return clone


def cleanup(*, force: bool = False) -> str:
    return remote.clean(
        "example-org/example-repo",
        "sample",
        "stamp",
        settings().sockets_dir,
        decision=remote.Cleanup("example-owner", force=force),
    )


@pytest.mark.parametrize(
    ("command", "expected"),
    [("exit 0", 0), ("exit 7", 7), ("kill -TERM $$", 143), ("kill -INT $$", 130)],
)
def test_mirror_lock_preserves_inner_exit(
    host: Path, command: str, expected: int
) -> None:
    mirror_in(host)
    tokens = shlex.split(
        remote.origin_branch(
            "example-org/example-repo", "sample", owner="example-owner"
        )
    )
    start = tokens.index("perl")
    result = subprocess.run(
        [*tokens[start : start + 4], str(host / "lock"), command],
        capture_output=True,
        check=False,
    )
    assert result.returncode == expected, result.stderr


@pytest.mark.parametrize(
    "step", ["worktree", "sample.phase", "sample.rc", "sample.owner"]
)
def test_clean_retries_each_step(host: Path, step: str) -> None:
    clone_in(host)
    no_session(host)
    state = host / "state/tbhbdev/agent-runs"
    (state / "sample.phase").write_text("ended\n")
    (state / "sample.rc").write_text("0\n")
    marker = host / "failed"
    write_standalone_fake(
        host / "bin/mv",
        "import os, sys\nfrom pathlib import Path\n"
        f"marker = Path({str(marker)!r})\nargs = sys.argv[1:]\nstep = {step!r}\n"
        "match = step in (Path(args[-1]).name, Path(args[0]).name)\n"
        "if match and not marker.exists():\n"
        "    marker.write_text('failed')\n    sys.exit(74)\n"
        "os.execv('/bin/mv', ['/bin/mv', *args])\n",
    )
    assert execute(cleanup()).returncode == 74
    assert execute(cleanup()).returncode == 0
    trash = state / "trash/stamp-sample"
    assert (trash / "worktree/agent-file").read_text() == "preserve"
    assert (trash / "sample.owner").read_text() == "example-owner\n"
    assert not list(state.glob("sample.*"))


@pytest.mark.parametrize("kind", ["file", "symlink", "dangling"])
@pytest.mark.parametrize("component", ["clone", ".worktrees", "agent-sample"])
def test_clean_refuses_unsafe_run_clone(host: Path, kind: str, component: str) -> None:
    clone = clone_in(host)
    no_session(host)
    tree = {
        "clone": clone,
        ".worktrees": clone / ".worktrees",
        "agent-sample": clone / ".worktrees/agent-sample",
    }[component]
    tree.rename(host / "saved-clone")
    if kind == "file":
        tree.write_text("preserve")
    else:
        tree.symlink_to(
            host / ("saved-clone" if kind == "symlink" else "missing"),
            target_is_directory=True,
        )
    result = execute(cleanup())
    assert result.returncode == 65
    assert b"clean refused" in result.stderr
    assert next((host / "saved-clone").rglob("agent-file")).read_text() == "preserve"


@pytest.mark.parametrize("kind", ["file", "symlink", "dangling"])
def test_claim_rejects_existing_owner(host: Path, kind: str) -> None:
    no_session(host)
    owner = host / "state/tbhbdev/agent-runs/sample.owner"
    owner.rename(host / "preserved-owner")
    if kind == "file":
        owner.write_text("other-owner\n")
    else:
        owner.symlink_to(host / ("preserved-owner" if kind == "symlink" else "missing"))
    result = execute(
        remote.transfer("sample", settings().sockets_dir, owner="example-owner")
    )
    assert result.returncode == 73
    assert (host / "preserved-owner").read_text() == "example-owner\n"
    assert (
        owner.is_symlink() if kind != "file" else owner.read_text() == "other-owner\n"
    )


def test_concurrent_claims_have_one_winner(host: Path) -> None:
    no_session(host)
    (host / "state/tbhbdev/agent-runs/sample.owner").rename(host / "preserved-owner")
    # Replace tar so the transfer reaches a successful end without input files.
    write_standalone_fake(host / "bin/tar", "")
    (host / "sockets").mkdir(mode=0o700)
    scripts = [
        remote.transfer("sample", settings().sockets_dir, owner=owner)
        for owner in ("first", "second")
    ]
    scripts = [
        s.replace(
            "PATH=/usr/bin:/bin:/usr/sbin:/sbin", "PATH=" + os.environ["PATH"]
        ).replace(settings().sockets_dir, str(host / "sockets"))
        for s in scripts
    ]
    processes = [
        subprocess.Popen(
            ["/bin/sh", "-c", s], stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        for s in scripts
    ]
    for process in processes:
        process.communicate(timeout=10)
    assert sorted(p.returncode for p in processes) == [0, 73]
    assert (host / "state/tbhbdev/agent-runs/sample.owner").read_text() in {
        "first\n",
        "second\n",
    }


def test_real_ssh_shell_quoting(host: Path) -> None:
    no_session(host)
    state = host / "state/tbhbdev/agent-runs"
    (state / "sample.log").write_text("literal ' $() ; bytes\n")
    script = remote.read("sample", settings().sockets_dir, owner="example-owner")
    script = script.replace(
        "PATH=/usr/bin:/bin:/usr/sbin:/sbin", "PATH=" + os.environ["PATH"]
    )
    script = script.replace(settings().sockets_dir, str(host / "sockets"))
    # The first shell parses the ssh command string; the second runs the script.
    result = subprocess.run(
        ["/bin/sh", "-c", shlex.join(("/bin/sh", "-c", script))],
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0
    assert result.stdout.endswith(b"literal ' $() ; bytes\n")


@pytest.mark.parametrize(
    "script",
    [
        "read",
        "fallback",
        "session",
        "keep",
        "clean",
        "origin_branch",
        "log",
        "diff",
        "start",
    ],
)
@pytest.mark.parametrize("owner_kind", ["other", "missing", "symlink"])
def test_owner_check_precedes_every_effect(
    host: Path, script: str, owner_kind: str
) -> None:
    owner = host / "state/tbhbdev/agent-runs/sample.owner"
    owner.rename(host / "saved-owner")
    if owner_kind == "other":
        owner.write_text("another-owner\n")
    elif owner_kind == "symlink":
        owner.symlink_to(host / "saved-owner")
    scripts = {
        "read": remote.read("sample", settings().sockets_dir, owner="example-owner"),
        "fallback": remote.fallback(
            "sample", settings().sockets_dir, owner="example-owner"
        ),
        "session": remote.session(
            "sample", "kill-session", settings().sockets_dir, owner="example-owner"
        ),
        "keep": remote.keep("sample", owner="example-owner"),
        "clean": cleanup(),
        "origin_branch": remote.origin_branch(
            "example-org/example-repo", "sample", owner="example-owner"
        ),
        "log": remote.inspect(
            "example-org/example-repo", "sample", owner="example-owner", limit=10
        ),
        "diff": remote.inspect(
            "example-org/example-repo", "sample", owner="example-owner", limit=None
        ),
        "start": remote.start(
            "sample", settings().sockets_dir, LINUX, owner="example-owner"
        )[2],
    }
    result = execute(scripts[script])
    assert result.returncode == 73
    assert not (host / "sockets").exists()
    assert not (host / "state/tbhbdev/agent-runs/trash").exists()


def test_locale_precedes_tmux_and_git(host: Path) -> None:
    mirror_in(host)
    for executable in ("tmux", "git"):
        write_standalone_fake(
            host / "bin" / executable,
            "import os, sys\n"
            "assert os.environ['LANG'] == 'C'\n"
            + (
                "assert os.environ['LC_ALL'] == 'C'\n"
                "sys.stderr.write('no server running on example')\nsys.exit(1)\n"
                if executable == "tmux"
                else "assert os.environ['GIT_NO_LAZY_FETCH'] == '1'\n"
                "assert 'fetch.recurseSubmodules=false' in sys.argv\n"
            ),
        )
    assert (
        execute(
            remote.read("sample", settings().sockets_dir, owner="example-owner")
        ).returncode
        == 0
    )
    assert (
        execute(
            remote.origin_branch(
                "example-org/example-repo", "sample", owner="example-owner"
            )
        ).returncode
        == 0
    )


def test_start_and_keep_with_matching_owner(host: Path) -> None:
    marker = host / "started"
    write_standalone_fake(
        host / "bin/tmux",
        "from pathlib import Path\nimport sys\n"
        "assert sys.argv[3:] == ['-f', '/dev/null', 'new-session', '-d', '-s',\n"
        "    'ag-sample', '/example path/bin/tbhbdev', 'alpha', 'agent', 'run-host',\n"
        "    'sample', '--host', 'example-linux']\n"
        f"Path({str(marker)!r}).write_text('started')\n",
    )
    config = replace(LINUX, tbhbdev="/example path/bin/tbhbdev")
    assert (
        execute(
            remote.start(
                "sample", settings().sockets_dir, config, owner="example-owner"
            )[2]
        ).returncode
        == 0
    )
    assert marker.read_text() == "started"
    assert execute(remote.keep("sample", owner="example-owner")).returncode == 0
    assert execute(remote.keep("sample", owner="example-owner")).returncode == 0
    assert (host / "state/tbhbdev/agent-runs/sample.keep").read_text() == ""


def test_preflight_ref_contract(host: Path) -> None:
    write_standalone_fake(host / "bin/version", "print('example version')\n")
    write_standalone_fake(
        host / "bin/git",
        "import os, sys\nfrom pathlib import Path\n"
        f"assert Path.cwd() == Path({str(host)!r})\n"
        "assert sys.argv[-6:] == ['ls-remote', '--exit-code', '--',\n"
        "    'git@github.com:example-org/example-repo.git', 'topic', 'topic^{}']\n"
        "assert 'protocol.ext.allow=never' in sys.argv\n"
        "assert os.environ['GIT_NO_LAZY_FETCH'] == '1'\n"
        "assert os.environ['LANG'] == os.environ['LC_ALL'] == 'C'\n"
        "print('commit\\ttopic')\n",
    )
    config = replace(LINUX, tbhbdev=str(host / "bin/version"))
    script = remote.preflight(
        "example-org/example-repo", "topic", "example version", config
    )
    result = execute(script)
    assert result.returncode == 0, result.stderr
    assert result.stdout == b"example version\ncommit\ttopic\n"


def test_origin_branch_queries_remote_presence(host: Path) -> None:
    mirror = mirror_in(host)
    script = remote.origin_branch(
        "example-org/example-repo", "sample", owner="example-owner"
    )
    result = execute(script)
    expected = git_in(mirror, "rev-parse", "refs/heads/agent/sample")
    assert result.returncode == 0, result.stderr
    assert result.stdout.decode() == expected + "\trefs/heads/agent/sample\n"
    git_in(host / "origin.git", "update-ref", "-d", "refs/heads/agent/sample")
    assert execute(script).stdout == b""
    assert git_in(mirror, "rev-parse", "refs/heads/agent/sample") == expected
    git_in(mirror, "config", "remote.origin.url", str(host / "missing.git"))
    assert execute(script).returncode != 0


def test_clean_checks_flags_fallback_and_missing_clone(host: Path) -> None:
    no_session(host)
    state = host / "state/tbhbdev/agent-runs"
    (state / "sample.rc.fallback").write_text("")
    assert execute(cleanup()).returncode == 65
    assert execute(cleanup(force=True)).returncode == 0
    assert (state / "trash/stamp-sample/sample.rc.fallback").is_file()


def test_origin_branch_without_mirror(host: Path) -> None:
    assert not (host / "state/tbhbdev/agent-mirrors").exists()
    result = execute(
        remote.origin_branch(
            "example-org/example-repo", "sample", owner="example-owner"
        )
    )
    assert result.returncode == 65
    assert result.stdout == b""


@pytest.mark.parametrize("limit", [None, 10])
def test_mirror_git_dir_never_discovers_parent(host: Path, limit: int | None) -> None:
    mirror = mirror_in(host)
    mirror.rename(host / "saved-mirror")
    mirror.mkdir()
    git_in(mirror.parent, "init")
    git_in(mirror.parent, "remote", "add", "origin", (host / "origin.git").as_uri())
    scripts = [
        remote.origin_branch(
            "example-org/example-repo", "sample", owner="example-owner"
        ),
        remote.inspect(
            "example-org/example-repo", "sample", owner="example-owner", limit=limit
        ),
    ]
    for script in scripts:
        result = execute(script)
        assert result.returncode != 0
        assert result.stdout == b""


@pytest.mark.parametrize("limit", [None, 10])
def test_inspection_fetch_does_not_write_metadata(
    host: Path, limit: int | None
) -> None:
    mirror = mirror_in(host)
    git_in(mirror, "config", "core.logAllRefUpdates", "true")
    fetch_head = mirror / "FETCH_HEAD"
    fetch_head.write_text("preserve fetch head\n")
    assert (
        execute(
            remote.inspect(
                "example-org/example-repo", "sample", owner="example-owner", limit=limit
            )
        ).returncode
        == 0
    )
    assert fetch_head.read_text() == "preserve fetch head\n"
    assert not (mirror / "logs").exists()


@pytest.mark.parametrize("limit", [None, 10])
@pytest.mark.parametrize("size", [0, 65535, 65536, 65537, 200000])
def test_inspection_caps_remote_output(
    host: Path, limit: int | None, size: int
) -> None:
    mirror_in(host)
    write_standalone_fake(
        host / "bin/git",
        f"import sys\nif 'fetch' not in sys.argv: sys.stdout.write('x' * {size})\n",
    )
    result = execute(
        remote.inspect(
            "example-org/example-repo", "sample", owner="example-owner", limit=limit
        )
    )
    assert result.returncode == 0, result.stderr
    expected = b"x" * min(size, 65536)
    if size > 65536:
        expected += b"\n[output truncated at 65536 bytes]\n"
    assert result.stdout == expected


@pytest.mark.parametrize("limit", [None, 10])
def test_inspection_preserves_read_failure(host: Path, limit: int | None) -> None:
    mirror_in(host)
    write_standalone_fake(
        host / "bin/git",
        "import sys\nsys.exit(0 if 'fetch' in sys.argv else 74)\n",
    )
    assert (
        execute(
            remote.inspect(
                "example-org/example-repo", "sample", owner="example-owner", limit=limit
            )
        ).returncode
        == 74
    )


@pytest.mark.parametrize("destination", ["worktree", "sample.phase"])
def test_clean_preserves_conflicting_trash(host: Path, destination: str) -> None:
    clone_in(host)
    no_session(host)
    state = host / "state/tbhbdev/agent-runs"
    (state / "sample.phase").write_text("ended\n")
    trash = state / "trash/stamp-sample"
    trash.mkdir(parents=True)
    conflict = trash / destination
    conflict.write_text("preserve existing trash")
    result = execute(cleanup())
    assert result.returncode == 65
    assert conflict.read_text() == "preserve existing trash"
    assert (state / "sample.owner").read_text() == "example-owner\n"


@pytest.mark.parametrize(
    ("component", "kind"),
    [
        (component, kind)
        for component in (
            "agent-mirrors",
            "github.com",
            "example-org",
            "example-repo.git",
        )
        for kind in ("symlink", "file", "missing")
    ]
    + [("agent-mirror.lock", "symlink")],
)
def test_mirror_refuses_symlinks(host: Path, component: str, kind: str) -> None:
    mirror = mirror_in(host)
    path = (
        mirror / component
        if component == "agent-mirror.lock"
        else next(p for p in (mirror, *mirror.parents) if p.name == component)
    )
    if path.exists():
        path.rename(host / "saved")
    if kind == "symlink":
        path.symlink_to(host / "saved")
    elif kind == "file":
        path.write_text("not a directory")
    result = execute(
        remote.origin_branch(
            "example-org/example-repo", "sample", owner="example-owner"
        )
    )
    assert result.returncode == 65
    assert result.stdout == b""


def test_mirror_path_separates_git_hosts(host: Path) -> None:
    mirror = mirror_in(host)
    other = (
        host
        / "state/tbhbdev/agent-mirrors/git.example.com/example-org/example-repo.git"
    )
    git_in(host, "clone", "--mirror", str(mirror), str(other))
    git_in(other, "remote", "set-url", "origin", str(host / "missing.git"))
    assert (
        execute(
            remote.origin_branch(
                "example-org/example-repo", "sample", owner="example-owner"
            )
        ).returncode
        == 0
    )
    assert (
        execute(
            remote.origin_branch(
                "git.example.com/example-org/example-repo",
                "sample",
                owner="example-owner",
            )
        ).returncode
        != 0
    )


def test_mirror_git_boundary(host: Path) -> None:
    mirror = mirror_in(host)
    write_standalone_fake(
        host / "bin/git",
        "import os, sys, fcntl\nfrom pathlib import Path\n"
        "assert os.environ['GIT_NO_LAZY_FETCH'] == '1'\n"
        "assert os.environ['GIT_CONFIG_NOSYSTEM'] == '1'\n"
        "assert os.environ['GIT_TERMINAL_PROMPT'] == '0'\n"
        "assert os.environ['GIT_ASKPASS'] == '/bin/false'\n"
        "assert os.environ['SSH_ASKPASS'] == '/bin/false'\n"
        "assert os.environ['LANG'] == os.environ['LC_ALL'] == 'C'\n"
        "options = {}\nargs = sys.argv[1:]\n"
        "while args and args[0] == '-c':\n"
        "    key, value = args[1].split('=', 1)\n"
        "    options[key] = value\n    args = args[2:]\n"
        "assert options == {\n"
        "    'core.hooksPath': '/dev/null', 'core.fsmonitor': 'false',\n"
        "    'core.sshCommand': 'ssh', 'credential.helper': '', 'core.askPass': '',\n"
        "    'protocol.ext.allow': 'never', 'core.pager': 'cat',\n"
        "    'gc.auto': '0', 'maintenance.auto': 'false',\n"
        "    'fetch.recurseSubmodules': 'false', 'submodule.recurse': 'false'}\n"
        f"assert args == [{('--git-dir=' + str(mirror))!r},\n"
        "                'ls-remote', '--end-of-options',\n"
        "                'origin', 'refs/heads/agent/sample']\n"
        f"with Path({str(mirror / 'agent-mirror.lock')!r}).open('a') as lock:\n"
        "    try:\n        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)\n"
        "    except BlockingIOError:\n        pass\n"
        "    else:\n        raise AssertionError('mirror lock required')\n"
        "os.execv('/usr/bin/git', ['/usr/bin/git', *sys.argv[1:]])\n",
    )
    result = execute(
        remote.origin_branch(
            "example-org/example-repo", "sample", owner="example-owner"
        )
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.endswith(b"\trefs/heads/agent/sample\n")


@pytest.mark.parametrize("limit", [None, 1, 10])
def test_inspection_reads_pushed_history(
    host: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    limit: int | None,
) -> None:
    mirror = mirror_in(host)
    source = host / "source"
    git_in(source, "checkout", "agent/sample")
    (source / "example").write_text("second pushed change\n")
    hostile_path = "path-\x1b[31m"
    (source / hostile_path).write_text("hostile file \x1b[33m\n")
    git_in(source, "add", "example", hostile_path)
    commit_in(source, "second change \x1b[31m", "Example \x1b[32m")
    git_in(source, "push", (host / "origin.git").as_uri(), "agent/sample")
    git_in(source, "checkout", "main")
    (source / "default-only").write_text("new default tip\n")
    git_in(source, "add", "default-only")
    commit_in(source, "default branch advanced")
    git_in(source, "push", (host / "origin.git").as_uri(), "main")
    # Even host settings that normally prune cannot drop mirror history.
    git_in(mirror, "branch", "preserved")
    git_in(mirror, "config", "fetch.prune", "true")
    git_in(mirror, "config", "core.quotePath", "false")
    # Git quotes control characters in paths, but stores the literal name.
    assert hostile_path in git_in(
        source, "ls-tree", "-z", "--name-only", "agent/sample"
    )
    raw = git_in(source, "show", "--format=raw", "agent/sample")
    assert "second change \x1b[31m" in raw
    assert "Example \x1b[32m" in raw
    output = inspect_output(monkeypatch, capsys, limit)
    assert "second" in output
    assert "default branch advanced" not in output
    assert "default-only" not in output
    assert "\x1b" not in output
    if limit is None:
        assert "-base\n+second pushed change\n" in output
        assert "+hostile file [33m\n" in output
    else:
        assert output.count("commit ") == (1 if limit == 1 else 2)
    assert git_in(mirror, "rev-parse", "refs/tbhbdev/default") == git_in(
        source, "rev-parse", "main"
    )
    assert git_in(mirror, "rev-parse", "preserved")


@pytest.mark.parametrize("verb", ["clean", "log", "diff"])
def test_run_clone_config_never_executes(host: Path, verb: str) -> None:
    mirror_in(host)
    clone = clone_in(host)
    tree = clone / ".worktrees/agent-sample"
    marker = host / "executed"
    command = host / "plant"
    command.write_text(f"#!/bin/sh\nprintf planted > {shlex.quote(str(marker))}\n")
    command.chmod(0o700)
    config = (
        f"[core]\nfsmonitor = {command}\npager = {command}\n"
        f"[alias]\nlog = !{command}\ndiff = !{command}\n"
        f'[filter "example"]\nclean = {command}\nsmudge = {command}\n'
        f"[diff]\nexternal = {command}\n"
    )
    (tree / ".git/config").write_text(config)
    hooks = tree / ".git/hooks"
    hooks.mkdir()
    (hooks / "post-index-change").symlink_to(command)
    (hooks / "reference-transaction").symlink_to(command)
    (clone / ".git").mkdir()
    (clone / ".git/config").write_text(config)
    # Any git at either agent-written location fails this test, even if hardened.
    write_standalone_fake(
        host / "bin/git",
        "import os, sys\n"
        f"assert {str(clone)!r} not in os.getcwd()\n"
        f"assert not any({str(clone)!r} in arg for arg in sys.argv)\n"
        "os.execv('/usr/bin/git', ['/usr/bin/git', *sys.argv[1:]])\n",
    )
    no_session(host)
    script = (
        cleanup()
        if verb == "clean"
        else remote.inspect(
            "example-org/example-repo",
            "sample",
            owner="example-owner",
            limit=10 if verb == "log" else None,
        )
    )
    assert execute(script).returncode == 0
    assert not marker.exists()
    assert (clone / ".git/config").read_text() == config
    if verb == "clean":
        assert (
            host / "state/tbhbdev/agent-runs/trash/stamp-sample/worktree/.git/config"
        ).read_text() == config


@pytest.mark.parametrize("limit", [None, 10])
def test_inspection_disables_external_diff_and_text_conversion(
    host: Path, limit: int | None
) -> None:
    mirror = mirror_in(host)
    marker = host / "executed"
    command = host / "plant"
    command.write_text(f"#!/bin/sh\nprintf planted > {shlex.quote(str(marker))}\n")
    command.chmod(0o700)
    git_in(mirror, "config", "diff.external", str(command))
    git_in(mirror, "config", "diff.example.textconv", str(command))
    (mirror / "info/attributes").write_text("* diff=example\n")
    script = remote.inspect(
        "example-org/example-repo", "sample", owner="example-owner", limit=limit
    )
    tokens = shlex.split(script)
    inner = tokens[-1]
    assert " -c diff.external= --no-optional-locks " in inner
    assert "--no-ext-diff --no-textconv --no-color" in inner
    assert "--no-prune --no-prune-tags" in inner
    assert execute(script).returncode == 0
    assert not marker.exists()


@pytest.mark.parametrize("limit", [None, 10])
def test_inspection_refuses_missing_origin_branch(
    host: Path, limit: int | None
) -> None:
    mirror_in(host)
    git_in(host / "origin.git", "update-ref", "-d", "refs/heads/agent/sample")
    result = execute(
        remote.inspect(
            "example-org/example-repo", "sample", owner="example-owner", limit=limit
        )
    )
    assert result.returncode != 0
    assert result.stdout == b""

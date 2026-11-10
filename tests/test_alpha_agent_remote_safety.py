"""Run the host scripts in scratch directories, through the real shell."""

from __future__ import annotations

import os
import shlex
import subprocess
from dataclasses import replace
from typing import TYPE_CHECKING

import pytest
from agent_driver_example import write_standalone_fake
from agent_example import LINUX, settings

from tbhbdev.alpha.agent import transport
from tbhbdev.alpha.agent.decisions import policy, remote

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    state = tmp_path / "state/tbhbdev/agent-runs"
    state.mkdir(parents=True)
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
    script = script.replace(settings().sockets_dir, os.environ["HOME"] + "/sockets")
    return subprocess.run(["/bin/sh", "-c", script], capture_output=True, check=False)


@pytest.mark.parametrize("error", ["unrelated failure", "missing"])
def test_tmux_error_never_writes_rc(host: Path, error: str) -> None:
    if error != "missing":
        write_standalone_fake(
            host / "bin/tmux",
            "import sys\nsys.stderr.write('unrelated failure')\nsys.exit(1)\n",
        )
    else:
        # A fixed PATH containing only scratch tools also tests a missing tmux.
        for name in ("mkdir", "chmod", "mv", "uname", "find", "id"):
            (host / "bin" / name).symlink_to("/bin/" + name)
        os.environ["PATH"] = str(host / "bin")
    result = execute(remote.fallback("sample", settings().sockets_dir))
    assert result.returncode != 0
    assert not (host / "state/tbhbdev/agent-runs/sample.rc").exists()


def test_session_is_checked_before_rc(host: Path) -> None:
    write_standalone_fake(
        host / "bin/tmux",
        "import os, sys\nfrom pathlib import Path\n"
        "root = Path(os.environ['XDG_STATE_HOME'])\n"
        "(root / 'tbhbdev/agent-runs/sample.rc').write_text('0')\n"
        'sys.stderr.write("can\'t find session: ag-sample\\n")\nsys.exit(1)\n',
    )
    result = execute(remote.read("sample", settings().sockets_dir))
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
            transport.read(settings(), LINUX, "sample", answer=answer)
    else:
        assert (
            transport.read(settings(), LINUX, "sample", answer=answer).text == expected
        )


@pytest.mark.parametrize("plant", ["fsmonitor", "hook", "smudge", "credential"])
def test_clean_never_executes_repo_plants(host: Path, plant: str) -> None:
    clone = host / "Code/github.com/example-org/example-repo"
    clone.mkdir(parents=True)

    def git(*args: str) -> None:
        subprocess.run(
            ["/usr/bin/git", "-C", str(clone), *args],
            check=True,
            capture_output=True,
            env={
                "HOME": str(host),
                "PATH": "/usr/bin:/bin",
                "LANG": "C",
                "GIT_CONFIG_NOSYSTEM": "1",
            },
        )

    git("init", "-b", "main")
    git(
        "-c",
        "user.name=Example",
        "-c",
        "user.email=example@example.com",
        "commit",
        "--allow-empty",
        "-m",
        "example",
    )
    tree = clone / ".worktrees/agent-sample"
    git("worktree", "add", "-b", "agent/sample", str(tree))
    marker = host / "marker"
    command = host / "plant"
    command.write_text(f"#!/bin/sh\nprintf planted > '{marker}'\n")
    command.chmod(0o700)
    if plant == "fsmonitor":
        git("config", "core.fsmonitor", str(command))
    elif plant == "hook":
        (clone / ".git/hooks/post-checkout").symlink_to(command)
    elif plant == "smudge":
        git("config", "filter.example.smudge", str(command))
        (clone / ".git/info/attributes").write_text("* filter=example\n")
    else:
        git("config", "credential.helper", str(command))
    # Test both clones' shared config and the agent's private worktree config.
    (tree / "agent-file").write_text("preserve")
    write_standalone_fake(
        host / "bin/tmux",
        'import sys\nsys.stderr.write("no server running on example")\nsys.exit(1)\n',
    )
    result = execute(
        remote.clean(
            "example-org/example-repo",
            "sample",
            "stamp",
            settings().sockets_dir,
            delete_branch=True,
        )
    )
    assert result.returncode == 0, result.stderr
    assert not marker.exists()
    assert not tree.exists()
    trash = host / "state/tbhbdev/agent-runs/trash/stamp-sample"
    assert (trash / "worktree/agent-file").read_text() == "preserve"
    assert not (clone / ".git/worktrees/agent-sample").exists()


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
    assert execute(remote.read("sample", settings().sockets_dir)).returncode == 65


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
        remote.keep("sample")
        if suffix == ".keep"
        else remote.fallback("sample", settings().sockets_dir)
    )
    assert execute(script).returncode != 0
    assert target.read_text() == "preserve"
    assert path.is_symlink()


def test_scripts_have_fixed_path_socket_and_git_environment() -> None:
    config = settings()
    scripts = [
        remote.session("sample", "kill-session", config.sockets_dir),
        remote.transfer("sample", config.sockets_dir),
        remote.start("sample", config.sockets_dir, LINUX)[2],
        remote.read("sample", config.sockets_dir),
        remote.read("sample", config.sockets_dir, answer=True),
        remote.fallback("sample", config.sockets_dir),
        remote.clean(
            "example-org/example-repo",
            "sample",
            "stamp",
            config.sockets_dir,
            delete_branch=True,
        ),
    ]
    for script in scripts:
        assert script.startswith("PATH=/usr/bin:/bin:/usr/sbin:/sbin\nexport PATH\n")
        assert 'tmux -S "$s/agents"' in script
        assert "tmux -L" not in script
        assert f"s={config.sockets_dir}\n" in script
    assert remote.installed(LINUX).endswith('"$HOME/.local/bin/tbhbdev" --version')
    assert "-lc" not in remote.start("sample", config.sockets_dir, LINUX)[2]
    for script in [
        remote.branch_facts("example-org/example-repo", "sample"),
        remote.preflight("example-org/example-repo", "main", "example", LINUX),
        scripts[-1],
    ]:
        for line in script.splitlines():
            if " git " in line:
                assert 'env -i PATH="$PATH" HOME="$HOME" LANG=C' in line
                assert "GIT_TERMINAL_PROMPT=0 GIT_CONFIG_NOSYSTEM=1" in line
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
    command = remote.start("example", "/example/sockets", LINUX)
    assert command[:2] == ("/bin/sh", "-c")
    assert shlex.split(command[2].splitlines()[-1]) == [
        "tmux",
        "-S",
        "$s/agents",
        "new-session",
        "-d",
        "-s",
        "ag-example",
        '"$HOME/.local/bin/tbhbdev" alpha agent run-host example',
    ]
    host = replace(LINUX, tbhbdev="/example path/bin/tbhbdev")
    assert remote.program(host) == "'/example path/bin/tbhbdev'"
    assert remote.installed(host).endswith("'/example path/bin/tbhbdev' --version")


def test_clone_path_is_not_the_worktree() -> None:
    script = remote.branch_facts("example-org/example-repo", "sample")
    assert (
        'cd "$HOME"/Code/github.com/example-org/example-repo || exit "$?"\n' in script
    )
    preflight = remote.preflight("example-org/example-repo", "main", "v", LINUX)
    assert 'cd "$HOME" || exit "$?"\n' in preflight
    assert (
        " ls-remote --exit-code -- git@github.com:example-org/example-repo.git "
        in preflight
    )
    assert "Code/" not in preflight


def test_read_defaults_to_bounded_log(host: Path) -> None:
    state = host / "state/tbhbdev/agent-runs"
    (state / "sample.log").write_bytes(b"L" * 70000)
    (state / "sample.out").write_bytes(b"O" * 70000)
    write_standalone_fake(
        host / "bin/tmux",
        'import sys\nsys.stderr.write("no server running on example")\nsys.exit(1)\n',
    )
    result = execute(remote.read("sample", settings().sockets_dir))
    assert result.returncode == 0
    assert result.stdout.split(b"\n", 4)[4] == b"L" * 65536
    result = execute(remote.read("sample", settings().sockets_dir, answer=True))
    assert result.returncode == 0
    assert result.stdout.split(b"\n", 4)[4] == b"O" * 65536


def test_claim_refusal_is_distinct_even_for_redirection_failure(host: Path) -> None:
    # A normal directory collision is checked before the exclusive creation.
    # Isolate the creation step too: sh treats ':' as a special builtin.
    claim = host / "existing"
    claim.write_text("preserve")
    script = remote.transfer("sample", settings().sockets_dir)
    line = next(
        line for line in script.splitlines() if ': > "$d/sample.request.json"' in line
    )
    line = line.replace('"$d/sample.request.json"', shlex.quote(str(claim)))
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
    result = execute(remote.read("sample", settings().sockets_dir))
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
    result = execute(remote.session("sample", "kill-session", settings().sockets_dir))
    assert result.returncode == 0
    assert not marker.exists()

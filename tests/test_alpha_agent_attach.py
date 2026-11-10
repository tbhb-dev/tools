"""Interactive clients use executable ssh and tmux stubs in a private directory."""

from __future__ import annotations

import dataclasses
import io
import os
import shlex
import subprocess
from typing import TYPE_CHECKING

import pytest
from agent_driver_example import execution_arguments, setup, write_standalone_fake
from agent_example import settings

from tbhbdev.alpha import agent
from tbhbdev.alpha.agent import attach, driver, transport
from tbhbdev.alpha.agent.decisions import remote as commands
from tbhbdev.alpha.agent.decisions.attach import command

if TYPE_CHECKING:
    from pathlib import Path

    from agent_driver_example import Remote

CONFIG = dataclasses.replace(
    settings(),
    ssh_connect_timeout=5,
    ssh_keepalive_seconds=7,
    ssh_keepalive_count_max=9,
)


@pytest.fixture
def remote(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Remote:
    result = setup(tmp_path, monkeypatch)
    monkeypatch.setattr(agent.settings, "load", lambda: CONFIG)
    program = result.bin / "ssh.py"
    program.write_text(
        program.read_text().replace(
            'if name == "ssh" else b""',
            'if name == "ssh" and "-t" not in args else b""',
        )
    )
    program = result.bin / "tmux.py"
    program.write_text(
        program.read_text().replace(
            '    elif verb == "kill-session":',
            '    elif verb == "attach-session":\n'
            '        print("terminal output")\n'
            '        print("terminal error", file=sys.stderr)\n'
            '        sys.exit(control.get("attach_rc", 0))\n'
            '    elif verb == "kill-session":',
        )
    )
    result.configure(session=True)
    return result


def record(
    remote: Remote, *, kind: str = "start", unsandboxed: str | None = None
) -> driver.Run:
    options = () if unsandboxed is None else ("--unsandboxed", unsandboxed)
    args = agent.parser("agent").parse_args(
        [kind, *execution_arguments(remote.prompt, *options)[1:]]
    )
    run = driver.prepare(CONFIG, args)
    driver.save(run)
    remote.file(".owner", run.owner + "\n")
    return run


def test_attach_refuses_exec(
    remote: Remote, capsys: pytest.CaptureFixture[str]
) -> None:
    record(remote, kind="exec")
    assert agent.main(["attach", "sample"], "agent") == 1
    assert capsys.readouterr() == (
        "",
        "sample: attach applies only to a start run; use logs\n",
    )
    assert not remote.records("ssh")


@pytest.mark.parametrize("read_only", [False, True])
@pytest.mark.parametrize("code", [0, 9, 255])
def test_attach_argv_and_inherited_streams(
    remote: Remote, capfd: pytest.CaptureFixture[str], read_only: bool, code: int
) -> None:
    run = record(remote)
    remote.configure(attach_rc=code)
    options = ["--read-only"] if read_only else []
    assert agent.main(["attach", "sample", *options], "agent") == code
    assert capfd.readouterr() == ("terminal output\n", "terminal error\n")
    script = (
        commands.owned("sample", run.owner)
        + commands.socket_root(CONFIG.sockets_dir)
        + "tmux_state has-session ag-sample\n"
        + "if test \"$present\" -eq 0; then printf '%s\\n' "
        + "'sample: no session; the run has ended' >&2; exit 1; fi\n"
        + 'tmux -S "$s/agents" -f /dev/null attach-session -t "ag-sample"'
        + (" -r" if read_only else "")
    )
    expected_command = shlex.join(
        ("/bin/bash", "-c", shlex.join(("/bin/sh", "-c", script)))
    )
    assert remote.records("ssh")[0]["args"] == [
        "-t",
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=5",
        "-o",
        "ServerAliveInterval=7",
        "-o",
        "ServerAliveCountMax=9",
        "-l",
        "agent",
        "example-linux",
        expected_command,
    ]
    assert remote.records("tmux")[-1]["args"] == [
        "-S",
        str(remote.root / "sockets/agents"),
        "-f",
        "/dev/null",
        "attach-session",
        "-t",
        "ag-sample",
        *(["-r"] if read_only else []),
    ]
    assert command(
        "sample", CONFIG.sockets_dir, owner=run.owner, read_only=read_only
    ) == ("/bin/sh", "-c", script)


def test_owner_gate_precedes_socket_setup(
    remote: Remote, capfd: pytest.CaptureFixture[str]
) -> None:
    record(remote)
    remote.file(".owner", "different-owner\n")
    assert not (remote.root / "sockets").exists()
    assert agent.main(["attach", "sample"], "agent") == 1
    assert capfd.readouterr() == ("", "agent: sample: not owner\n")
    assert not remote.records("tmux")
    assert not (remote.root / "sockets").exists()
    assert not driver.metadata_path("sample").exists()


def test_no_session(remote: Remote, capfd: pytest.CaptureFixture[str]) -> None:
    record(remote)
    remote.configure(session=False)
    assert agent.main(["attach", "sample"], "agent") == 1
    assert capfd.readouterr() == ("", "sample: no session; the run has ended\n")
    assert len(remote.records("tmux")) == 1


def test_attach_collision_reports_owner_error(remote: Remote) -> None:
    run = record(remote)
    remote.configure(attach_rc=transport.COLLISION)
    with pytest.raises(transport.CollisionError, match=r"^sample: not owner$"):
        attach.run(CONFIG, run, read_only=False)


def test_attach_unsandboxed(remote: Remote, capfd: pytest.CaptureFixture[str]) -> None:
    record(remote, unsandboxed="o/r#1")
    assert agent.main(["attach", "sample"], "agent") == 0
    assert capfd.readouterr() == (
        "terminal output\n",
        "sample: sandbox=none authorized_by=o/r#1\nterminal error\n",
    )


def test_refusal_filters_name(
    remote: Remote, capsys: pytest.CaptureFixture[str]
) -> None:
    run = record(remote, kind="exec")
    run.name = "na\x1bme"
    assert attach.run(CONFIG, run, read_only=False) == 1
    assert capsys.readouterr() == (
        "",
        "name: attach applies only to a start run; use logs\n",
    )


def test_client_inherits_input(
    remote: Remote, capfd: pytest.CaptureFixture[str]
) -> None:
    run = record(remote)
    write_standalone_fake(remote.bin / "ssh", "import sys\nprint(sys.stdin.read())\n")
    input_file = remote.root / "input"
    input_file.write_text("typed input")
    with input_file.open() as stream:
        previous = os.dup(0)
        try:
            os.dup2(stream.fileno(), 0)
            assert attach.run(CONFIG, run, read_only=False) == 0
        finally:
            os.dup2(previous, 0)
            os.close(previous)
    assert capfd.readouterr() == ("typed input\n", "")


def test_unsandboxed_notice_is_flushed_before_ssh(
    remote: Remote, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = record(remote, unsandboxed="o/r#1")
    buffer = io.BytesIO()
    stream = io.TextIOWrapper(buffer, encoding="utf-8")

    def run_ssh(
        args: tuple[str, ...], *, check: bool
    ) -> subprocess.CompletedProcess[bytes]:
        assert check is False
        assert buffer.getvalue() == b"sample: sandbox=none authorized_by=o/r#1\n"
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(attach.sys, "stderr", stream)
    monkeypatch.setattr(attach.subprocess, "run", run_ssh)
    assert attach.run(CONFIG, run, read_only=False) == 0

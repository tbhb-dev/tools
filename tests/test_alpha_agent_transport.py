"""Transport failures and bounds, without reaching an external host."""

from __future__ import annotations

import asyncio
import dataclasses
import io
import os
import shlex
import tarfile
from typing import TYPE_CHECKING

import pytest
from agent_driver_example import write_standalone_fake
from agent_example import LINUX, settings

from tbhbdev.alpha.agent import model, transport
from tbhbdev.alpha.agent.decisions import remote

if TYPE_CHECKING:
    from collections.abc import Awaitable
    from pathlib import Path


def fixed_reading(monkeypatch: pytest.MonkeyPatch, payload: str) -> None:
    def shell(*_args: object, **_kwargs: object) -> str:
        return payload

    monkeypatch.setattr(transport, "shell", shell)


@pytest.mark.parametrize("error", [OSError("example"), TimeoutError("example")])
def test_transport_exceptions(
    monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    async def fail(*_args: object, **_kwargs: object) -> tuple[int, bytes]:
        raise error

    monkeypatch.setattr(transport, "execute", fail)
    with pytest.raises(transport.TransportError) as caught:
        transport.call(settings(), LINUX, ("true",))
    assert str(caught.value) == "example-linux: transport failed"
    assert caught.value.__cause__ is error


@pytest.mark.parametrize(("timeout", "expected"), [(1.5, 1.5), (0, 0.001), (-1, 0.001)])
def test_transport_bounds(
    monkeypatch: pytest.MonkeyPatch, timeout: float, expected: float
) -> None:
    async def run(args: tuple[str, ...], data: bytes) -> tuple[int, bytes]:
        expected_argv = shlex.split(
            "/usr/bin/ssh -o BatchMode=yes -o ConnectTimeout=15 "
            "-o ServerAliveInterval=30 "
            "-o ServerAliveCountMax=3 -l agent example-linux"
        )
        assert args == (*expected_argv, "/bin/bash -c true")
        assert data == b"example"
        return 0, b"example-output"

    async def wait_for(
        future: Awaitable[tuple[int, bytes]], seconds: float
    ) -> tuple[int, bytes]:
        assert seconds == expected
        return await future

    monkeypatch.setattr(transport.asyncio, "wait_for", wait_for)
    monkeypatch.setattr(transport, "execute", run)
    assert (
        transport.call(settings(), LINUX, ("true",), data=b"example", timeout=timeout)
        == "example-output"
    )


def test_remote_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fail(_args: tuple[str, ...], _data: bytes) -> tuple[int, bytes]:
        return 255, b"private remote output"

    monkeypatch.setattr(transport, "execute", fail)
    with pytest.raises(transport.TransportError) as caught:
        transport.call(settings(), LINUX, ("true",))
    assert str(caught.value) == "example-linux: remote command failed (255)"


@pytest.mark.parametrize("timeout", [None, 7.5])
def test_shell_forwards_inputs(
    monkeypatch: pytest.MonkeyPatch, timeout: float | None
) -> None:
    def call(
        config: model.Settings,
        host: model.Host,
        command: tuple[str, ...],
        *,
        data: bytes,
        timeout: float,
    ) -> str:
        assert config == settings()
        assert host == LINUX
        assert command == ("/bin/sh", "-c", "example")
        assert data == b"payload"
        assert timeout == (7.5 if custom else 30)
        return "output"

    custom = timeout is not None
    monkeypatch.setattr(transport, "call", call)
    kwargs: dict[str, float] = {} if timeout is None else {"timeout": timeout}
    assert (
        transport.shell(settings(), LINUX, "example", data=b"payload", **kwargs)
        == "output"
    )


def test_shell_default_data(monkeypatch: pytest.MonkeyPatch) -> None:
    def call(
        _config: model.Settings,
        _host: model.Host,
        _command: tuple[str, ...],
        *,
        data: bytes,
        timeout: float,
    ) -> str:
        assert data == b""
        assert timeout == 30
        return ""

    monkeypatch.setattr(transport, "call", call)
    assert transport.shell(settings(), LINUX, "true") == ""


@pytest.mark.parametrize("schema", [None, '{"type":"object"}'])
def test_stream_modes_and_budget(
    monkeypatch: pytest.MonkeyPatch, schema: str | None
) -> None:
    def shell(
        config: model.Settings,
        host: model.Host,
        script: str,
        *,
        data: bytes,
        timeout: float,
    ) -> str:
        assert config == settings()
        assert host == LINUX
        assert script == remote.transfer("example", settings().sockets_dir)
        assert timeout == 4.5
        with tarfile.open(fileobj=io.BytesIO(data)) as archive:
            members = archive.getmembers()
            assert [m.name for m in members] == [
                "example.request.json",
                "example.prompt",
                *(("example.schema.json",) if schema else ()),
            ]
            assert all(m.mode == 0o600 for m in members)
            contents = []
            for member in members:
                stream = archive.extractfile(member)
                assert stream is not None
                contents.append(stream.read().decode())
            assert contents == ["request", "brief é", *((schema,) if schema else ())]
        return ""

    monkeypatch.setattr(transport, "shell", shell)
    transport.stream(
        settings(),
        LINUX,
        "example",
        inputs=transport.Inputs("request", "brief é", schema),
        timeout=4.5,
    )


@pytest.mark.parametrize("timeout", [None, 7.5])
@pytest.mark.parametrize("answer", [False, True])
def test_read_budget(
    monkeypatch: pytest.MonkeyPatch, timeout: float | None, answer: bool
) -> None:
    def shell(
        config: model.Settings, host: model.Host, script: str, *, timeout: float
    ) -> str:
        assert config == settings()
        assert host == LINUX
        assert script == remote.read("example", settings().sockets_dir, answer=answer)
        assert timeout == (7.5 if custom else 30)
        return "66696e6973686564\n30\n1\n0\nanswer\n"

    custom = timeout is not None
    monkeypatch.setattr(transport, "shell", shell)
    kwargs: dict[str, float] = {} if timeout is None else {"timeout": timeout}
    assert transport.read(
        settings(), LINUX, "example", answer=answer, **kwargs
    ) == transport.Reading("finished", 0, launched=True, session=False, text="answer\n")


def test_call_defaults_and_connection_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    config = dataclasses.replace(
        settings(),
        ssh_connect_timeout=5,
        ssh_keepalive_seconds=7,
        ssh_keepalive_count_max=9,
    )

    async def run(args: tuple[str, ...], data: bytes) -> tuple[int, bytes]:
        expected = shlex.split(
            "/usr/bin/ssh -o BatchMode=yes -o ConnectTimeout=5 "
            "-o ServerAliveInterval=7 "
            "-o ServerAliveCountMax=9 -l agent example-linux"
        )
        assert args == (*expected, "/bin/bash -c true")
        assert data == b""
        return 0, b""

    async def wait_for(
        future: Awaitable[tuple[int, bytes]], seconds: float
    ) -> tuple[int, bytes]:
        assert seconds == 30
        return await future

    monkeypatch.setattr(transport.asyncio, "wait_for", wait_for)
    monkeypatch.setattr(transport, "execute", run)
    assert transport.call(config, LINUX, ("true",)) == ""


def test_exec_streams_stdin_and_captures_both_outputs(
    tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    program = tmp_path / "example-tool"
    write_standalone_fake(
        program,
        (
            "import sys\nsys.stdout.buffer.write(sys.stdin.buffer.read())\n"
            'sys.stderr.write("example-error")\n'
        ),
    )
    assert asyncio.run(
        transport.execute((str(program), "argument"), b"example-input")
    ) == (0, b"example-input")
    assert capfd.readouterr() == ("", "")


def test_timeout_reaps_only_its_ssh_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    program = tmp_path / "ssh"
    pid_file = tmp_path / "pid"
    write_standalone_fake(
        program,
        (
            "import os\nimport time\nfrom pathlib import Path\n"
            f"Path({str(pid_file)!r}).write_text(str(os.getpid()))\n"
            "time.sleep(2)\n"
        ),
    )

    def executable(_name: str) -> str:
        return str(program)

    monkeypatch.setattr(transport.argv, "executable", executable)
    with pytest.raises(transport.TransportError, match="transport failed"):
        transport.call(settings(), LINUX, ("true",), timeout=0.5)
    pid = int(pid_file.read_text())
    with pytest.raises(ChildProcessError):
        os.waitpid(pid, os.WNOHANG)


@pytest.mark.parametrize("rc", ["0", "1", "255", "000", "255\n"])
@pytest.mark.parametrize("phase", ["running", "a" * 32, "running\n"])
def test_valid_status_boundaries(
    monkeypatch: pytest.MonkeyPatch, rc: str, phase: str
) -> None:
    payload = f"{phase.encode().hex()}\n{rc.encode().hex()}\n1\n0\nanswer\tline\n"
    fixed_reading(monkeypatch, payload)
    reading = transport.read(settings(), LINUX, "sample")
    assert reading.rc == int(rc)
    assert reading.phase == phase.removesuffix("\n")
    assert reading.text == "answer\tline\n"


@pytest.mark.parametrize(
    ("phase", "rc", "field"),
    [
        ("RUNNING", "30", "phase"),
        ("", "30", "phase"),
        ("a" * 33, "30", "phase"),
        ("running", "", "rc"),
        ("running", b"-1".hex(), "rc"),
        ("running", b"0000".hex(), "rc"),
        ("running", b"1\n0".hex(), "rc"),
    ],
)
def test_invalid_status_fields_have_a_named_error(
    monkeypatch: pytest.MonkeyPatch, phase: str, rc: str, field: str
) -> None:
    payload = f"{phase.encode().hex()}\n{rc}\n1\n0\n"
    fixed_reading(monkeypatch, payload)
    with pytest.raises(model.Invalid, match=f"^sample: {field}=invalid$"):
        transport.read(settings(), LINUX, "sample")


def test_collision_is_not_a_transport_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    async def refuse(_args: tuple[str, ...], _data: bytes) -> tuple[int, bytes]:
        return 73, b"untrusted output"

    monkeypatch.setattr(transport, "execute", refuse)
    with pytest.raises(transport.CollisionError) as caught:
        transport.call(settings(), LINUX, ("true",))
    assert str(caught.value) == "example-linux: run name collision"
    assert not isinstance(caught.value, transport.TransportError)


@pytest.mark.parametrize("session", ["0", "1"])
def test_read_absent_rc_and_default_view(
    monkeypatch: pytest.MonkeyPatch, session: str
) -> None:
    def shell(
        _config: model.Settings, _host: model.Host, script: str, *, timeout: float
    ) -> str:
        assert script == remote.read("sample", settings().sockets_dir, answer=False)
        assert timeout == 30
        return f"72756e6e696e67\n-\n0\n{session}\n"

    monkeypatch.setattr(transport, "shell", shell)
    assert transport.read(settings(), LINUX, "sample") == transport.Reading(
        "running", None, launched=False, session=session == "1", text=""
    )


@pytest.mark.parametrize("field", ["phase", "rc"])
def test_read_invalid_utf8_status(monkeypatch: pytest.MonkeyPatch, field: str) -> None:
    phase = "ff" if field == "phase" else b"finished".hex()
    rc = "ff" if field == "rc" else "30"
    fixed_reading(monkeypatch, f"{phase}\n{rc}\n0\n0\n")
    with pytest.raises(model.Invalid, match=f"^sample: {field}=invalid$"):
        transport.read(settings(), LINUX, "sample")


def test_execute_preserves_nonzero_status(tmp_path: Path) -> None:
    program = tmp_path / "example-tool"
    write_standalone_fake(program, "import sys\nprint('failed')\nsys.exit(9)\n")
    assert asyncio.run(transport.execute((str(program),), b"")) == (9, b"failed\n")

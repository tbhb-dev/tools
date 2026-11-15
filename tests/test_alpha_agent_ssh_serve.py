"""The forced-command protocol uses only disposable state and fake accounts."""

from __future__ import annotations

import io
import json
import os
import pwd
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from agent_host_example import build_provenance

from tbhbdev.alpha import agent
from tbhbdev.alpha.agent import build_info, process_group, run_host, runfiles, ssh_serve
from tbhbdev.alpha.agent.decisions.run_state import Group


@pytest.fixture(autouse=True)
def isolated(
    monkeypatch: pytest.MonkeyPatch, isolated_agent_home: None, agent_suffix_case: None
) -> None:
    del isolated_agent_home, agent_suffix_case
    monkeypatch.setattr(pwd, "getpwuid", account)


def incoming(monkeypatch: pytest.MonkeyPatch, value: bytes) -> None:
    monkeypatch.setattr("sys.stdin", io.TextIOWrapper(io.BytesIO(value)))


@pytest.mark.parametrize("token", [None, "synthetic-push-value"])
def test_preflight_envelope(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    token: str | None,
) -> None:
    monkeypatch.setenv("SSH_ORIGINAL_COMMAND", "preflight example")
    incoming(
        monkeypatch, json.dumps({"v": 1, "request": {}, "push_token": token}).encode()
    )
    calls: list[list[str]] = []

    def main(arguments: list[str], prog: str, **kwargs: object) -> int:
        calls.append(arguments)
        assert prog == "ssh-serve preflight"
        assert kwargs == {"push_token": None, "claimed": True}
        return 0

    monkeypatch.setattr(run_host, "main", main)
    assert ssh_serve.main([], "ssh-serve") == (0 if token is None else 2)
    files = files_for("example")
    if token is None:
        assert calls == [["example", "--preflight"]]
        assert files.path("request.json").read_text() == "{}\n"
        assert files.path("owner").read_text() == "host\n"
        assert capsys.readouterr().err == ""
    else:
        assert calls == []
        assert not files.path("request.json").exists()
        assert not files.path("owner").exists()
        assert capsys.readouterr().err == (
            "ssh-serve: refused: preflight requires a null push_token\n"
        )


@pytest.mark.parametrize(
    "command",
    [
        None,
        "",
        " ",
        "run-host",
        "result",
        "kill",
        "sh x",
        "result x extra",
        "result x;",
        "result x ;",
        "result x && true",
        "result `x`",
        "result $(x)",
        "result 'x",
        "result ../x",
        "result " + "x" * 65,
    ],
)
def test_refused(
    command: str | None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    if command is None:
        monkeypatch.delenv("SSH_ORIGINAL_COMMAND", raising=False)
    else:
        monkeypatch.setenv("SSH_ORIGINAL_COMMAND", command)
    assert ssh_serve.main([], "ssh-serve") == 2
    output = capsys.readouterr()
    assert output.out == ""
    assert output.err.startswith("ssh-serve: refused: ")
    assert list(Path.cwd().rglob("*.request.json")) == []


@pytest.mark.parametrize("command", ["run-host x", "result x", "kill x"])
def test_wrong_account(
    command: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("SSH_ORIGINAL_COMMAND", command)
    monkeypatch.setattr(pwd, "getpwuid", other_account)
    assert agent.main(["ssh-serve"], "agent") == 2
    assert capsys.readouterr().err == "ssh-serve: refused: requires agent account\n"


@pytest.mark.parametrize(
    "body",
    [
        b"x",
        b"[]",
        b"{}",
        b'{"v":2,"request":{},"push_token":null}',
        b'{"v":true,"request":{},"push_token":null}',
        b'{"v":1,"request":[],"push_token":null}',
        b'{"v":1,"request":{},"push_token":3}',
        b'{"v":1,"request":{},"push_token":"a\\nb"}',
        b" " * (1024 * 1024 + 1),
        b"\xff",
    ],
)
def test_bad_envelope(
    body: bytes, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("SSH_ORIGINAL_COMMAND", "run-host x")
    incoming(monkeypatch, body)
    assert ssh_serve.main([], "ssh-serve") == 2
    assert capsys.readouterr().err.startswith("ssh-serve: refused: ")


def test_run_dispatch(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("SSH_ORIGINAL_COMMAND", "run-host Example_1")
    document = {"v": 1, "request": {"v": 2}, "push_token": "synthetic-push-secret"}
    incoming(monkeypatch, json.dumps(document).encode())

    def run(
        arguments: list[str],
        prog: str,
        *,
        push_token: str | None = None,
        claimed: bool = False,
    ) -> int:
        assert claimed is True
        assert files_for("Example_1").path("owner").read_text() == "host\n"
        assert arguments == ["Example_1"]
        assert prog == "ssh-serve run-host"
        assert push_token == document["push_token"]
        files = files_for("Example_1")
        assert (
            files.path("request.json").read_text()
            == json.dumps(document["request"]) + "\n"
        )
        assert files.path("request.json").stat().st_mode & 0o777 == 0o600
        sys.stdout.write("normal run output\n")
        return 7

    monkeypatch.setattr(run_host, "main", run)
    assert ssh_serve.main([], "ssh-serve") == 7
    assert capsys.readouterr().out == "normal run output\n"
    for path in Path.cwd().rglob("*"):
        if path.is_file():
            assert "synthetic-push-secret" not in path.read_text()


@pytest.mark.parametrize("command", ["result", "kill"])
def test_unknown(command: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SSH_ORIGINAL_COMMAND", f"{command} missing")
    assert ssh_serve.main([], "ssh-serve") == 3


@pytest.mark.parametrize("finished", [False, True])
def test_result(
    finished: bool, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("SSH_ORIGINAL_COMMAND", "result example")
    files = files_for("example")
    files.write("request.json", "{}")
    files.write("phase", "ended" if finished else "running")
    files.write("log", "".join(f"line {n}\n" for n in range(210)))
    if finished:
        files.write("rc", "4\n")
        files.write("out.final", "answer")
        files.write("branch", "agent/example")
        files.write("head", "a" * 40)
    assert ssh_serve.main([], "ssh-serve") == 0
    assert json.loads(capsys.readouterr().out) == {
        **build_info.identities(),
        "v": 1,
        "name": "example",
        "rc": 4 if finished else None,
        "state": "ended" if finished else "running",
        "report": "answer" if finished else None,
        "log_tail": "".join(f"line {n}\n" for n in range(10, 210)),
        "branch": "agent/example" if finished else None,
        "head": "a" * 40 if finished else None,
    }


@pytest.mark.parametrize("finished", [False, True])
def test_kill(
    finished: bool, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("SSH_ORIGINAL_COMMAND", "kill example")
    files = files_for("example")
    files.write("request.json", "{}")
    files.write("pgid", json.dumps({"pgid": 98765, "boot": "boot", "start": "start"}))
    if finished:
        files.write("rc", "0")
    calls: list[int] = []
    monkeypatch.setattr(process_group, "identity", group_identity)

    def stop(files: runfiles.RunFiles, pid: int, grace: float) -> bool:
        assert files.name == "example"
        assert grace == 0
        calls.append(pid)
        return True

    monkeypatch.setattr(process_group, "stop_owned", stop)
    assert ssh_serve.main([], "ssh-serve") == 0
    assert calls == ([] if finished else [98765])
    assert capsys.readouterr().out == ("already finished\n" if finished else "")


def files_for(name: str) -> runfiles.RunFiles:
    return runfiles.RunFiles(
        runfiles.state_home(Path.home()) / "tbhbdev" / "agent-runs", name
    )


def account(uid: int) -> SimpleNamespace:
    assert uid == os.getuid()
    return SimpleNamespace(pw_name="agent")


def other_account(_uid: int) -> SimpleNamespace:
    return SimpleNamespace(pw_name="other")


def group_identity(pid: int) -> Group:
    return Group(pid, "boot", "start")


def test_local_arguments(capsys: pytest.CaptureFixture[str]) -> None:
    assert ssh_serve.main(["extra"], "ssh-serve") == 2
    assert (
        capsys.readouterr().err == "ssh-serve: refused: ssh-serve takes no arguments\n"
    )


@pytest.mark.parametrize("owned", [False, True])
def test_kill_refuses_unowned_or_unstoppable(
    owned: bool, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("SSH_ORIGINAL_COMMAND", "kill example")
    files = files_for("example")
    files.write("request.json", "{}")
    files.write("pgid", json.dumps({"pgid": 98765, "boot": "boot", "start": "start"}))
    monkeypatch.setattr(
        process_group, "identity", group_identity if owned else absent_group
    )
    monkeypatch.setattr(process_group, "stop_owned", cannot_stop)
    assert ssh_serve.main([], "ssh-serve") == 2
    assert capsys.readouterr().err == (
        "ssh-serve: refused: cannot signal run group\n"
        if owned
        else "ssh-serve: refused: run group identity no longer matches\n"
    )


def absent_group(_pid: int) -> None:
    return None


def cannot_stop(*_args: object) -> bool:
    return False


@pytest.mark.parametrize("token", [None, "", "a\r", "a\0"])
def test_token_values(token: str | None, monkeypatch: pytest.MonkeyPatch) -> None:
    incoming(
        monkeypatch, json.dumps({"v": 1, "request": {}, "push_token": token}).encode()
    )
    if token is None:
        assert ssh_serve.envelope() == ({}, None)
    else:
        with pytest.raises(ValueError, match="single line"):
            ssh_serve.envelope()


def test_input_boundary(monkeypatch: pytest.MonkeyPatch) -> None:
    body = b'{"v":1,"request":{},"push_token":null}'
    incoming(monkeypatch, body + b" " * (1024 * 1024 - len(body)))
    assert ssh_serve.envelope() == ({}, None)


def test_os_error_is_generic(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("SSH_ORIGINAL_COMMAND", "kill example")
    files_for("example").write("request.json", "{}")
    assert ssh_serve.main([], "ssh-serve") == 2
    assert (
        capsys.readouterr().err == "ssh-serve: refused: invalid command or run data\n"
    )


def test_launch_key_constants() -> None:
    assert (
        Path("/Library/Application Support/tbhbdev/agent-launch/id_ed25519")
        == ssh_serve.MACOS_LAUNCH_KEY
    )
    assert Path("/etc/tbhbdev/agent-launch/id_ed25519") == ssh_serve.LINUX_LAUNCH_KEY


def test_result_before_first_phase(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("SSH_ORIGINAL_COMMAND", "result example")
    files_for("example").write("request.json", "{}")
    assert ssh_serve.main([], "ssh-serve") == 0
    assert json.loads(capsys.readouterr().out) == {
        **build_info.identities(),
        "v": 1,
        "name": "example",
        "rc": None,
        "state": None,
        "report": None,
        "log_tail": "",
        "branch": None,
        "head": None,
    }


def test_existing_request_is_preserved(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    files = files_for("example")
    files.write("request.json", "original")
    monkeypatch.setenv("SSH_ORIGINAL_COMMAND", "run-host example")
    incoming(monkeypatch, b'{"v":1,"request":{},"push_token":null}')
    assert ssh_serve.main([], "ssh-serve") == 2
    assert (
        capsys.readouterr().err == "ssh-serve: refused: invalid command or run data\n"
    )
    assert files.path("request.json").read_text() == "original"


@pytest.mark.parametrize(
    ("command", "reason"),
    [
        ("", "expected run-host NAME, preflight NAME, result NAME or kill NAME"),
        ("result ../x", "invalid run name"),
        ("result 'x", "invalid command or run data"),
    ],
)
def test_command_refusal_reason(
    command: str,
    reason: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("SSH_ORIGINAL_COMMAND", command)
    assert ssh_serve.main([], "ssh-serve") == 2
    assert capsys.readouterr().err == f"ssh-serve: refused: {reason}\n"


@pytest.mark.parametrize(
    ("data", "reason"),
    [
        ({}, "expected v, request and push_token"),
        ({"v": 2, "request": {}, "push_token": None}, "unsupported envelope version"),
        ({"v": 1, "request": [], "push_token": None}, "request must be an object"),
        (
            {"v": 1, "request": {}, "push_token": 3},
            "push_token must be a nonempty single line or null",
        ),
    ],
)
def test_envelope_refusal_reason(
    data: dict[str, object],
    reason: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("SSH_ORIGINAL_COMMAND", "run-host example")
    incoming(monkeypatch, json.dumps(data).encode())
    assert ssh_serve.main([], "ssh-serve") == 2
    assert capsys.readouterr().err == f"ssh-serve: refused: {reason}\n"


def test_oversized_refusal_reason(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("SSH_ORIGINAL_COMMAND", "run-host example")
    incoming(monkeypatch, b" " * (1024 * 1024 + 1))
    assert ssh_serve.main([], "ssh-serve") == 2
    assert capsys.readouterr().err == "ssh-serve: refused: stdin exceeds 1 MiB\n"


def test_agent_dispatch_forwards_arguments(monkeypatch: pytest.MonkeyPatch) -> None:
    def serve(arguments: list[str], prog: str) -> int:
        assert arguments == ["extra"]
        assert prog == "alpha agent ssh-serve"
        return 9

    monkeypatch.setattr(ssh_serve, "main", serve)
    assert agent.main(["ssh-serve", "extra"], "alpha agent") == 9


def test_missing_account_is_refused(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def missing(_uid: int) -> None:
        raise KeyError

    monkeypatch.setattr(pwd, "getpwuid", missing)
    monkeypatch.setenv("SSH_ORIGINAL_COMMAND", "result example")
    assert ssh_serve.main([], "ssh-serve") == 2
    assert (
        capsys.readouterr().err == "ssh-serve: refused: invalid command or run data\n"
    )


def test_invalid_group_data_is_refused(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("SSH_ORIGINAL_COMMAND", "kill example")
    files = files_for("example")
    files.write("request.json", "{}")
    files.write("pgid", "[]")
    assert ssh_serve.main([], "ssh-serve") == 2
    assert (
        capsys.readouterr().err == "ssh-serve: refused: invalid command or run data\n"
    )


def test_existing_owner_refuses_before_request(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    files = files_for("example")
    files.write("owner", "existing owner")
    monkeypatch.setenv("SSH_ORIGINAL_COMMAND", "run-host example")
    incoming(monkeypatch, b'{"v":1,"request":{},"push_token":null}')
    assert ssh_serve.main([], "ssh-serve") == 2
    assert (
        capsys.readouterr().err
        == "ssh-serve: refused: run 'example' already has an owner\n"
    )
    assert files.path("owner").read_text() == "existing owner"
    assert not files.path("request.json").exists()


def test_missing_command_reports_invalid_data(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("SSH_ORIGINAL_COMMAND", raising=False)
    assert ssh_serve.main([], "ssh-serve") == 2
    assert capsys.readouterr().err == (
        "ssh-serve: refused: invalid command or run data\n"
    )


def test_oversized_input_reads_only_one_lookahead_byte(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = io.BytesIO(b" " * (2 * ssh_serve.INPUT_LIMIT))
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(source))
    with pytest.raises(ValueError, match=r"^stdin exceeds 1 MiB$"):
        ssh_serve.envelope()
    assert source.tell() == ssh_serve.INPUT_LIMIT + 1


def test_token_with_uppercase_character(monkeypatch: pytest.MonkeyPatch) -> None:
    value = "synthetic-X-push-value"
    incoming(
        monkeypatch, json.dumps({"v": 1, "request": {}, "push_token": value}).encode()
    )
    assert ssh_serve.envelope() == ({}, value)


def test_private_run_files_ignore_permissive_umask(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SSH_ORIGINAL_COMMAND", "run-host example")
    incoming(monkeypatch, b'{"v":1,"request":{},"push_token":null}')

    def run(*_args: object, **_kwargs: object) -> int:
        return 0

    monkeypatch.setattr(run_host, "main", run)
    previous = os.umask(0)
    try:
        assert ssh_serve.main([], "ssh-serve") == 0
    finally:
        os.umask(previous)
    files = files_for("example")
    for suffix in ("owner", "request.json"):
        assert files.path(suffix).stat().st_mode & 0o777 == 0o600


def test_default_state_directory_is_canonical(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    monkeypatch.setenv("SSH_ORIGINAL_COMMAND", "result example")
    expected = Path.home() / ".local" / "state" / "tbhbdev" / "agent-runs"
    files_for("example").write("request.json", "{}")

    def result(files: runfiles.RunFiles) -> int:
        assert files.root == expected
        assert files.name == "example"
        return 0

    monkeypatch.setattr(ssh_serve, "result", result)
    assert ssh_serve.main([], "ssh-serve") == 0


@pytest.mark.parametrize("present", [False, True])
def test_result_reports_build_provenance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    *,
    present: bool,
) -> None:
    expected = build_provenance(tmp_path, monkeypatch, present=present)
    monkeypatch.setenv("SSH_ORIGINAL_COMMAND", "result example")
    files_for("example").write("request.json", "{}")
    assert ssh_serve.main([], "ssh-serve") == 0
    data = json.loads(capsys.readouterr().out)
    assert {key: data[key] for key in expected} == expected


def test_deeply_nested_envelope_is_refused(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("SSH_ORIGINAL_COMMAND", "run-host example")
    incoming(monkeypatch, b"[" * 100000 + b"0" + b"]" * 100000)
    assert ssh_serve.main([], "ssh-serve") == 2
    assert capsys.readouterr().err == ("ssh-serve: refused: invalid envelope\n")
    assert not files_for("example").path("request.json").exists()


def refused_untouched(
    capsys: pytest.CaptureFixture[str], reason: str = "invalid envelope"
) -> None:
    assert capsys.readouterr().err == f"ssh-serve: refused: {reason}\n"
    assert not files_for("example").path("request.json").exists()
    assert not files_for("example").path("owner").exists()


def test_depth_is_checked_before_the_decoder_runs(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def decoder_must_not_run(_text: str) -> None:
        raise AssertionError

    monkeypatch.setenv("SSH_ORIGINAL_COMMAND", "run-host example")
    monkeypatch.setattr(ssh_serve.json, "loads", decoder_must_not_run)
    incoming(monkeypatch, b"[" * 33 + b"0" + b"]" * 33)
    assert ssh_serve.main([], "ssh-serve") == 2
    refused_untouched(capsys)


def test_decoder_recursion_error_is_refused(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def overflow(_text: str) -> None:
        raise RecursionError

    monkeypatch.setenv("SSH_ORIGINAL_COMMAND", "run-host example")
    monkeypatch.setattr(ssh_serve.json, "loads", overflow)
    incoming(monkeypatch, b"{}")
    assert ssh_serve.main([], "ssh-serve") == 2
    refused_untouched(capsys)


@pytest.mark.parametrize("encoding", ["utf-16", "utf-16-le", "utf-32"])
def test_non_utf8_envelope_is_refused(
    encoding: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    text = (
        json.dumps(
            {
                "v": 1,
                "request": {"a": "[" * 100000 + "0" + "]" * 100000},
                "push_token": None,
            }
        )
        .replace('"[', "[")
        .replace(']"', "]")
    )
    monkeypatch.setenv("SSH_ORIGINAL_COMMAND", "run-host example")
    incoming(monkeypatch, text.encode(encoding))
    assert ssh_serve.main([], "ssh-serve") == 2
    refused_untouched(capsys)


def test_unserializable_request_is_refused_before_claiming(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def overflow(_value: object) -> None:
        raise RecursionError

    monkeypatch.setenv("SSH_ORIGINAL_COMMAND", "run-host example")
    monkeypatch.setattr(ssh_serve.json, "dumps", overflow)
    incoming(monkeypatch, b'{"v": 1, "request": {}, "push_token": null}')
    assert ssh_serve.main([], "ssh-serve") == 2
    refused_untouched(capsys, "invalid command or run data")


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("[" * 32 + "]" * 32, False),
        ("[" * 33 + "]" * 33, True),
        ("{" * 33 + "}" * 33, True),
        ('["' + "[" * 40 + '"]', False),
        ('["\\\\"' + "[" * 40 + '"]', True),
        ('["\\"' + "[" * 40 + '"]', False),
        ("", False),
        ("[]" * 40, False),
        ("X" * 40 + "[" * 33, True),
        ("X" * 40, False),
        ("[" * 32 + "]" + "[[", True),
        ('"a"' * 40 + "[" * 33, True),
        ('"a"' * 40, False),
        ("{}" * 40, False),
        ('"' + "[" * 40, False),
        ('"\\"' + "[" * 33, False),
        ("\\" + "[" * 33, True),
    ],
)
def test_too_deep(text: str, *, expected: bool) -> None:
    assert ssh_serve.too_deep(text) is expected


def test_too_deep_is_linear() -> None:
    assert not ssh_serve.too_deep('"' + "\\" * 500000 + '"')

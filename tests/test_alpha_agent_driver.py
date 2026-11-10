"""Coordinator verbs through executable ssh and tmux fakes on PATH."""

from __future__ import annotations

import base64
import dataclasses
import datetime as dt
import io
import json
import os
import shlex
import tarfile
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest
from agent_driver_example import (
    Remote,
    execution_arguments,
    git_in,
    mirror_in,
    setup,
    write_standalone_fake,
)
from agent_example import MARKER, settings

from tbhbdev import version
from tbhbdev.alpha import agent
from tbhbdev.alpha.agent import driver, model, render, transport
from tbhbdev.alpha.agent.decisions import remote as remote_commands
from tbhbdev.alpha.agent.decisions import request, runfiles_names
from tbhbdev.alpha.agent.model import Invalid, RunKind

if TYPE_CHECKING:
    from pathlib import Path

CONFIG = settings(reference=f"{MARKER}/{MARKER}/{MARKER}")
COMMIT = "0123456789abcdef0123456789abcdef01234567"


@pytest.fixture
def remote(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Remote:
    result = setup(tmp_path, monkeypatch)
    monkeypatch.setattr(agent.settings, "load", lambda: CONFIG)

    def no_sleep(_seconds: float) -> None:
        pass

    ticks = iter(range(0, 200, 10))
    monkeypatch.setattr(
        driver, "time", SimpleNamespace(monotonic=lambda: next(ticks), sleep=no_sleep)
    )
    return result


def arguments(remote: Remote, *options: str) -> list[str]:
    return execution_arguments(remote.prompt, *options)


def launch(remote: Remote, *options: str) -> int:
    return agent.main(arguments(remote, *options), "example agent")


def command(verb: str, *options: str) -> int:
    return agent.main([verb, "sample", *options], "example agent")


@pytest.mark.parametrize("verb", ["log", "diff", "clean"])
def test_unsandboxed_refuses_inspection_before_ssh(
    remote: Remote, capsys: pytest.CaptureFixture[str], verb: str
) -> None:
    assert launch(remote, "--ops", "--unsandboxed", "o/r#1") == 0
    before = {tool: remote.records(tool) for tool in ("ssh", "git", "tmux")}
    capsys.readouterr()
    assert command(verb) == 65
    message = (
        "unsandboxed run: pass --force to move it to trash"
        if verb == "clean"
        else "unsandboxed run: inspect agent/sample on GitHub"
    )
    assert capsys.readouterr() == ("", f"{message}\n")
    assert {tool: remote.records(tool) for tool in before} == before


def test_unsandboxed_force_clean_only_moves_to_trash(
    remote: Remote, capsys: pytest.CaptureFixture[str]
) -> None:
    assert launch(remote, "--ops", "--unsandboxed", "o/r#1") == 0
    remote.configure(session=False)
    before = {tool: remote.records(tool) for tool in ("ssh", "git", "tmux")}
    capsys.readouterr()
    assert command("clean", "--force") == 0
    assert capsys.readouterr() == ("", "")
    assert remote.records("git") == before["git"]
    assert len(remote.records("tmux")) == len(before["tmux"]) + 2
    calls = remote.records("ssh")
    assert len(calls) == len(before["ssh"]) + 2
    script = str(calls[-1]["args"])
    assert "agent-mirrors" not in script
    assert "tmux_state has-session ag-sample" in script
    assert " git " not in script
    assert not driver.metadata_path("sample").exists()
    trash = next((remote.state / "trash").iterdir())
    assert trash.name.endswith("-sample")
    assert (trash / "worktree").is_dir()
    assert (trash / "sample.owner").is_file()


@pytest.mark.parametrize("verb", ["status", "wait", "unreachable", "finished"])
def test_unsandboxed_run_is_visible(
    remote: Remote,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    verb: str,
) -> None:
    if verb == "finished":
        remote.configure(launch={"rc": "0"})
    assert launch(remote, "--ops", "--unsandboxed", "o/r#1") == 0
    suffix = " sandbox=none authorized_by=o/r#1\n"
    assert suffix in capsys.readouterr().out
    if verb == "finished":
        return
    if verb == "wait":
        remote.file(".rc", "0")
        remote.configure(session=False)
    if verb == "unreachable":
        remote.configure(ssh=[{"rc": 255}] * 10)
        ticking(monkeypatch, 240)
    assert command("wait" if verb == "unreachable" else verb) == (
        75 if verb == "unreachable" else 0
    )
    assert suffix in capsys.readouterr().out


def test_launch_stream_and_metadata(
    remote: Remote, capsys: pytest.CaptureFixture[str]
) -> None:
    schema = remote.root / "schema.json"
    schema.write_text('{"type":"object"}')
    assert (
        launch(
            remote,
            "--schema",
            str(schema),
            "--class",
            "test",
            "--variant",
            "one",
            "--role",
            "worker",
        )
        == 0
    )
    assert capsys.readouterr() == (
        "sample: launched host=example-linux account=agent\n",
        "",
    )
    data = json.loads((remote.state / "sample.request.json").read_text())
    parsed = request.parse(data, host_allows_unsandboxed=True)
    assert request.to_dict(parsed) == data
    assert (parsed.class_, parsed.variant, parsed.role) == ("test", "one", "worker")
    assert parsed.commit is None
    assert parsed.account == "agent"
    assert parsed.timeout is None
    assert parsed.startup_deadline_seconds == 120
    assert (remote.state / "sample.prompt").read_text() == remote.prompt.read_text()
    assert (remote.state / "sample.schema.json").read_text() == schema.read_text()
    assert remote.state.stat().st_mode & 0o777 == 0o700
    for suffix in (".request.json", ".prompt", ".schema.json"):
        assert (remote.state / ("sample" + suffix)).stat().st_mode & 0o777 == 0o600
    meta = remote.local / "tbhbdev/agent-runs/sample.json"
    record = json.loads(meta.read_text())
    assert record == {
        **data,
        "host": "example-linux",
        "started": record["started"],
        "owner": record["owner"],
        "cleaning": False,
    }
    assert len(record["owner"]) == 32
    assert (remote.state / "sample.owner").read_text() == record["owner"] + "\n"
    assert record["started"].endswith("+00:00")
    assert meta.stat().st_mode & 0o777 == 0o600
    assert meta.parent.stat().st_mode & 0o777 == 0o700
    calls = remote.records("ssh")
    assert len(calls) == 4
    archive = base64.b64decode(str(calls[1]["stdin"]))
    with tarfile.open(fileobj=io.BytesIO(archive)) as stream:
        assert stream.getnames() == [
            "sample.request.json",
            "sample.prompt",
            "sample.schema.json",
        ]
    for tool in ("ssh", "tmux"):
        assert MARKER not in json.dumps(remote.records(tool))
    assert MARKER not in meta.read_text()
    tmux = remote.records("tmux")[0]["args"]
    assert tmux == shlex.split(
        f"-S {remote.root}/sockets/agents -f /dev/null has-session -t =ag-sample"
    )


@pytest.mark.parametrize(
    ("options", "message"),
    [
        (("--ref", "main"), "--ref applies only to a review run"),
        (("--repo", "bare"), "must be org/repo"),
        (("--host", "missing"), "host 'missing' isn't configured"),
        (("--account", "missing"), "account 'missing' isn't one"),
        (("--claude-profile", "primary"), "applies only to a claude run"),
        (
            (
                "--harness",
                "claude",
                "--reason",
                "example",
                "--claude-profile",
                "Missing",
            ),
            "profile 'Missing' isn't defined",
        ),
        (("--harness", "claude"), "reason is required"),
        (("--unsandboxed", "bad"), "request authorized_by must be"),
        (
            ("--unsandboxed", "o/r#1", "--host", "example-macos"),
            "isn't enabled on this host",
        ),
        (
            ("--unsandboxed", "o/r#1", "--profile", "review"),
            "applies only to a build run",
        ),
        (
            ("--unsandboxed", "o/r#1", "--harness", "claude", "--reason", "example"),
            "applies only to a codex run",
        ),
        (("--harness", "agy"), "agy requires a gemini-*"),
        (("--model", "bad model"), "model must match"),
        (("--effort", "bad effort"), "effort must match"),
    ],
)
def test_checks_before_ssh(
    remote: Remote,
    capsys: pytest.CaptureFixture[str],
    options: tuple[str, ...],
    message: str,
) -> None:
    assert launch(remote, *options) == 1
    assert message in capsys.readouterr().err
    assert remote.records("ssh") == []


def test_reused_name(remote: Remote, capsys: pytest.CaptureFixture[str]) -> None:
    assert launch(remote) == 0
    before = remote.records("ssh")
    assert launch(remote) == 1
    assert "clean first" in capsys.readouterr().err
    assert remote.records("ssh") == before


@pytest.mark.parametrize("name", ["../bad", "-bad", "a b", "a" * 65, ""])
def test_bad_name(
    remote: Remote, capsys: pytest.CaptureFixture[str], name: str
) -> None:
    args = arguments(remote)
    args[-2] = name
    assert agent.main(args, "example agent") in (1, 2)
    assert remote.records("ssh") == []
    assert capsys.readouterr().err


def test_version_mismatch(remote: Remote, capsys: pytest.CaptureFixture[str]) -> None:
    remote.configure(version="tbhbdev example-other")
    assert launch(remote) == 1
    message = capsys.readouterr().err
    assert "version mismatch" in message
    assert version.line() in message
    assert "tbhbdev example-other" in message
    assert len(remote.records("ssh")) == 1


@pytest.mark.parametrize("ref", ["topic", "refs/pull/2/head", COMMIT, "HEAD"])
def test_review_ref(remote: Remote, ref: str) -> None:
    remote.configure(refs=f"{COMMIT}\t{ref}\n")
    options = ("--ref", ref) if ref != "HEAD" else ()
    assert launch(remote, "--profile", "review", *options) == 0
    assert (
        json.loads((remote.state / "sample.request.json").read_text())["commit"]
        == COMMIT
    )
    calls = remote.records("ssh")
    assert "--version" in str(calls[0]["args"])
    git = remote.records("git")
    if ref == COMMIT:
        assert git == []
    else:
        assert git == [
            {
                "args": [
                    *shlex.split(remote_commands.GIT_FLAGS)[1:],
                    "ls-remote",
                    "--exit-code",
                    "--",
                    "git@github.com:example-org/example-repo.git",
                    ref,
                    ref + "^{}",
                ],
                "stdin": "",
            }
        ]


@pytest.mark.parametrize("profile", [None, "Primary", "PRIMARY"])
def test_claude_case(remote: Remote, profile: str | None) -> None:
    options = () if profile is None else ("--claude-profile", profile)
    assert (
        launch(remote, "--harness", "claude", "--reason", "example-gap", *options) == 0
    )
    assert (
        json.loads((remote.state / "sample.request.json").read_text())["claude_profile"]
        == "primary"
    )


@pytest.mark.parametrize(
    ("options", "timeout"),
    [
        (("--ops", "--unsandboxed", "o/r#1"), None),
        (("--harness", "agy", "--model", "gemini-example"), 900),
        (("--harness", "agy", "--allow-non-gemini", "--timeout", "1h"), 3600),
    ],
)
def test_request_options(
    remote: Remote, options: tuple[str, ...], timeout: int | None
) -> None:
    assert launch(remote, *options) == 0
    record = json.loads((remote.state / "sample.request.json").read_text())
    assert record["timeout"] == timeout
    if "--ops" in options:
        assert record["account"] == "ops"
        assert record["unsandboxed"] is True
        assert record["authorized_by"] == "o/r#1"
        assert "-l', 'ops'" in str(remote.records("ssh")[0]["args"])


@pytest.mark.parametrize("rc", [0, 7])
def test_rc_first(remote: Remote, capsys: pytest.CaptureFixture[str], rc: int) -> None:
    remote.configure(launch={"rc": f"{rc}\n", "log": "setup stopped\n"})
    assert launch(remote) == rc
    assert capsys.readouterr() == (f"sample: rc={rc}\nsetup stopped\n", "")
    assert remote.records("tmux")[-1]["args"] == [
        "-S",
        str(remote.root / "sockets/agents"),
        "-f",
        "/dev/null",
        "kill-session",
        "-t",
        "=ag-sample",
    ]


def ticking(monkeypatch: pytest.MonkeyPatch, step: float = 100) -> None:
    ticks = iter(range(20))
    monkeypatch.setattr(
        driver,
        "time",
        SimpleNamespace(monotonic=lambda: next(ticks) * step, sleep=driver.time.sleep),
    )


def test_launch_deadline(
    remote: Remote, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Leave time for the input calls, then cross the deadline during polling.
    ticks = iter([0, 0, 0, 0, 0, 120, 120, 120, 134, 134, 134])
    monkeypatch.setattr(
        driver,
        "time",
        SimpleNamespace(monotonic=lambda: next(ticks), sleep=driver.time.sleep),
    )
    remote.configure(launch={"phase": "preparing"}, kill_rc="124\n")
    assert launch(remote) == 1
    assert (
        "launch unconfirmed host=example-linux phase=preparing"
        in capsys.readouterr().out
    )
    assert (remote.state / "sample.rc").read_text() == "124\n"


@pytest.mark.parametrize("final", [True, False])
def test_wait_success(
    remote: Remote, capsys: pytest.CaptureFixture[str], final: bool
) -> None:
    assert launch(remote) == 0
    capsys.readouterr()
    remote.file(".rc", "4\n")
    remote.file(".out", "stream answer\n")
    if final:
        remote.file(".out.final", "final answer\n")
    assert command("wait") == 4
    assert capsys.readouterr() == (
        "sample: rc=4\n"
        + ("final answer\n" if final else "stream answer\n")
        + "\n"
        + render.INSPECTION_WARNING
        + "\n",
        "",
    )


def test_wait_unreachable(
    remote: Remote, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert launch(remote) == 0
    capsys.readouterr()
    remote.configure(ssh=[{"rc": 255}] * 4)
    ticking(monkeypatch)
    assert command("wait") == 75
    assert capsys.readouterr() == (
        "sample: rc=unreachable\n" + render.INSPECTION_WARNING + "\n",
        "",
    )


def test_wait_unknown(remote: Remote, capsys: pytest.CaptureFixture[str]) -> None:
    assert launch(remote) == 0
    capsys.readouterr()
    remote.configure(session=False)
    assert command("wait") == 1
    assert capsys.readouterr() == (
        "sample: rc=unknown\n\n" + render.INSPECTION_WARNING + "\n",
        "",
    )


@pytest.mark.parametrize(
    ("session", "rc", "expected"),
    [(True, None, "running"), (False, "0\n", "0"), (False, None, "unknown")],
)
def test_status(
    remote: Remote,
    capsys: pytest.CaptureFixture[str],
    session: bool,
    rc: str | None,
    expected: str,
) -> None:
    assert launch(remote) == 0
    capsys.readouterr()
    remote.configure(session=session)
    remote.file(".phase", "running\n")
    if rc is not None:
        remote.file(".rc", rc)
    before = len(remote.records("ssh"))
    assert command("status") == 0
    assert len(remote.records("ssh")) == before + 1
    output = capsys.readouterr()
    assert output.err == ""
    started = json.loads(driver.metadata_path("sample").read_text())["started"]
    assert output.out == (
        f"sample: phase=running rc={expected} host=example-linux "
        f"account=agent started={started}\n"
    )


def test_kill_host_rc(remote: Remote) -> None:
    assert launch(remote) == 0
    remote.configure(kill_rc="143\n")
    assert command("kill") == 0
    assert (remote.state / "sample.rc").read_text() == "143\n"
    assert not (remote.state / "sample.rc.pending").exists()
    assert not any(
        ".rc.fallback" in str(record["args"]) for record in remote.records("ssh")
    )


def test_kill_fallback(remote: Remote, monkeypatch: pytest.MonkeyPatch) -> None:
    assert launch(remote) == 0
    ticking(monkeypatch)
    assert command("kill") == 0
    assert (remote.state / "sample.rc").read_text() == "137\n"
    assert (remote.state / "sample.rc").stat().st_mode & 0o777 == 0o600


def test_clean_running(remote: Remote, capsys: pytest.CaptureFixture[str]) -> None:
    assert launch(remote) == 0
    assert command("clean", "--force") == 1
    assert "clean refused" in capsys.readouterr().err
    assert remote.records("git") == []


@pytest.mark.parametrize("force", [False, True])
def test_clean(remote: Remote, force: bool) -> None:
    assert launch(remote) == 0
    remote.configure(session=False)
    remote.file(".rc", "0")
    assert command("clean", *(("--force",) if force else ())) == 0
    assert not (remote.local / "tbhbdev/agent-runs/sample.json").exists()
    assert list((remote.local / "tbhbdev/agent-runs/trash").glob("*/sample.json"))
    assert not (remote.state / "sample.request.json").exists()
    assert list((remote.state / "trash").glob("*/sample.request.json"))
    calls = [record["args"] for record in remote.records("git")]
    assert len(calls) == (0 if force else 1)
    assert not any("branch" in str(call) for call in calls)


@pytest.mark.parametrize("failure", ["absent", "missing-mirror", "remote-error"])
def test_clean_origin_failure_requires_force(
    remote: Remote, capsys: pytest.CaptureFixture[str], failure: str
) -> None:
    assert launch(remote) == 0
    capsys.readouterr()
    remote.configure(
        session=False,
        branch=failure != "absent",
        origin_error=failure == "remote-error",
    )
    if failure == "missing-mirror":
        mirror = remote.root / "state/tbhbdev/agent-mirrors"
        mirror.rename(remote.root / "saved-mirrors")
    remote.file(".rc", "0")
    assert command("clean") == 1
    output = capsys.readouterr()
    if failure == "absent":
        assert output.err == (
            "example agent: branch agent/sample does not exist on origin; "
            "pass --force to clean anyway\n"
        )
    assert driver.metadata_path("sample").exists()
    assert (remote.state / "sample.owner").is_file()
    assert command("clean", "--force") == 0


def test_clean_unknown_needs_force(
    remote: Remote, capsys: pytest.CaptureFixture[str]
) -> None:
    assert launch(remote) == 0
    remote.configure(session=False)
    assert command("clean") == 1
    assert "unknown; clean requires --force" in capsys.readouterr().err


def test_keep(remote: Remote) -> None:
    assert launch(remote) == 0
    assert command("keep") == 0
    assert (remote.state / "sample.keep").read_text() == ""
    assert (remote.state / "sample.keep").stat().st_mode & 0o777 == 0o600


def test_wait_start_refused(remote: Remote, capsys: pytest.CaptureFixture[str]) -> None:
    assert launch(remote) == 0
    record = driver.load(CONFIG, "sample")
    path = driver.metadata_path("sample")
    path.rename(path.with_suffix(".saved"))
    record.request = dataclasses.replace(record.request, kind=RunKind.START)
    driver.save(record)
    assert command("wait") == 1
    assert "wait applies only to an exec run" in capsys.readouterr().err


def test_launch_polls_then_acknowledges(remote: Remote) -> None:
    remote.configure(ssh=[None, None, None, {"out": "preparing\n\n0\n1\n"}, None])
    assert launch(remote) == 0
    assert len(remote.records("ssh")) == 5


def test_stream_transport_failure_never_kills(
    remote: Remote, capsys: pytest.CaptureFixture[str]
) -> None:
    remote.configure(ssh=[None, {"rc": 255}], kill_rc="1\n")
    assert launch(remote) == 1
    assert "not owner" in capsys.readouterr().err
    assert not driver.metadata_path("sample").exists()
    assert not (remote.state / "sample.rc").exists()


def test_wait_retries_and_resets_failure_time(
    remote: Remote, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert launch(remote) == 0
    capsys.readouterr()
    remote.configure(
        ssh=[
            {"rc": 255},
            {"out": "running\n\n1\n1\n"},
            {"rc": 255},
            {"out": "finished\n0\n1\n0\nanswer\n"},
        ]
    )
    ticking(monkeypatch, 40)
    monkeypatch.setenv("TBHB_AGENT_WAIT_POLL", "0.1")
    monkeypatch.setenv("TBHB_AGENT_WAIT_UNREACHABLE", "240")
    assert command("wait") == 0
    assert capsys.readouterr() == (
        "sample: rc=0\nanswer\n\n" + render.INSPECTION_WARNING + "\n",
        "",
    )


def test_kill_refuses_live_session(
    remote: Remote, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert launch(remote) == 0
    remote.configure(ssh=[{"out": ""}, {"out": "running\n\n1\n1\n"}])
    ticking(monkeypatch)
    assert command("kill") == 1
    assert (
        "session still exists; return code left to run-host" in capsys.readouterr().err
    )
    assert not (remote.state / "sample.rc").exists()


def test_clean_reports_only_origin_presence(
    remote: Remote, capsys: pytest.CaptureFixture[str]
) -> None:
    assert launch(remote) == 0
    capsys.readouterr()
    remote.configure(session=False)
    remote.file(".rc", "0")
    assert command("clean") == 0
    assert capsys.readouterr() == ("branch exists on origin\n", "")


def test_kill_waits_for_host_rc(
    remote: Remote, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert launch(remote) == 0
    remote.configure(
        ssh=[None, {"out": "stopping\n\n1\n0\n"}, {"out": "finished\n143\n1\n0\n"}]
    )
    ticking(monkeypatch, 1)
    assert command("kill") == 0
    assert not any(
        ".rc.fallback" in str(record["args"]) for record in remote.records("ssh")
    )


@pytest.mark.parametrize(
    "flag", ["--host", "--repo", "--model", "--effort", "--profile"]
)
def test_required_flags(remote: Remote, flag: str) -> None:
    args = arguments(remote)
    index = args.index(flag)
    del args[index : index + 2]
    assert agent.main(args, "example agent") == 2
    assert not remote.records("ssh")


def test_schema_and_prompt_refused_locally(remote: Remote) -> None:
    bad = remote.root / "bad.json"
    bad.write_text("{")
    assert launch(remote, "--schema", str(bad)) == 1
    assert remote.records("ssh") == []
    remote.prompt.rename(remote.prompt.with_suffix(".saved"))
    assert launch(remote) == 1
    assert remote.records("ssh") == []


def test_metadata_round_trip_and_exclusive_create(remote: Remote) -> None:
    options = ("--ops", "--unsandboxed", "o/r#1")
    assert launch(remote, *options) == 0
    run = driver.load(CONFIG, "sample")
    assert run.host.account == "ops"
    assert run.request.account == "ops"
    assert run.request.unsandboxed
    assert run.request.authorized_by == "o/r#1"
    with pytest.raises(FileExistsError):
        driver.save(run)


@pytest.mark.parametrize("xdg", [None, ""])
def test_metadata_name_and_default_state(
    remote: Remote, monkeypatch: pytest.MonkeyPatch, xdg: str | None
) -> None:

    with pytest.raises(Invalid, match="run name"):
        driver.metadata_path("../bad")
    if xdg is None:
        monkeypatch.delenv("XDG_STATE_HOME")
    else:
        monkeypatch.setenv("XDG_STATE_HOME", xdg)
    monkeypatch.setenv("HOME", str(remote.root))
    assert driver.directory() == remote.root / ".local/state/tbhbdev/agent-runs"


@pytest.mark.parametrize("poll", ["0.1", "2.5"])
def test_wait_poll_and_transport_budget(
    remote: Remote,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    poll: str,
) -> None:
    assert launch(remote) == 0
    capsys.readouterr()
    monkeypatch.setenv("TBHB_AGENT_WAIT_POLL", poll)
    monkeypatch.setenv("TBHB_AGENT_WAIT_UNREACHABLE", "9")
    ticking(monkeypatch, 1)
    sleeps: list[float] = []
    monkeypatch.setattr(driver.time, "sleep", sleeps.append)
    readings = iter(
        [
            None,
            transport.Reading("running", None, launched=True, session=True, text=""),
            None,
            transport.Reading(
                "finished", 0, launched=True, session=False, text="answer\n"
            ),
        ]
    )
    timeouts: list[float] = []

    def read(
        config: model.Settings,
        host: model.Host,
        name: tuple[str, str],
        *,
        answer: bool,
        timeout: float,
    ) -> transport.Reading:
        assert config == CONFIG
        assert host == CONFIG.hosts[0]
        assert name == ("sample", driver.load(CONFIG, "sample").owner)
        assert answer is True
        timeouts.append(timeout)
        reading = next(readings)
        if reading is None:
            message = "example"
            raise transport.TransportError(message)
        return reading

    monkeypatch.setattr(transport, "read", read)
    assert command("wait") == 0
    assert timeouts == [9, 7, 9, 7]
    assert sleeps == [max(0.5, float(poll))] * 3
    assert capsys.readouterr() == (
        "sample: rc=0\nanswer\n\n" + render.INSPECTION_WARNING + "\n",
        "",
    )


def test_wait_defaults_and_failure_deadline(
    remote: Remote, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert launch(remote) == 0
    capsys.readouterr()
    monkeypatch.delenv("TBHB_AGENT_WAIT_POLL", raising=False)
    monkeypatch.delenv("TBHB_AGENT_WAIT_UNREACHABLE", raising=False)
    ticks = iter([0, 0, 240, 240])
    sleeps: list[float] = []
    monkeypatch.setattr(
        driver,
        "time",
        SimpleNamespace(monotonic=lambda: next(ticks), sleep=sleeps.append),
    )
    timeouts: list[float] = []

    def read(
        _config: model.Settings,
        _host: model.Host,
        _name: tuple[str, str],
        *,
        answer: bool,
        timeout: float,
    ) -> transport.Reading:
        assert answer is True
        timeouts.append(timeout)
        message = "example"
        raise transport.TransportError(message)

    monkeypatch.setattr(transport, "read", read)
    assert command("wait") == 75
    assert capsys.readouterr() == (
        "sample: rc=unreachable\n" + render.INSPECTION_WARNING + "\n",
        "",
    )
    assert sleeps == [30]
    assert timeouts == [30, 0]


@pytest.mark.parametrize(
    "name", ["TBHB_AGENT_WAIT_POLL", "TBHB_AGENT_WAIT_UNREACHABLE"]
)
def test_wait_invalid_environment(
    remote: Remote,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    name: str,
) -> None:
    assert launch(remote) == 0
    capsys.readouterr()
    monkeypatch.setenv(name, "bad")
    before = remote.records("ssh")
    assert command("wait") == 1
    assert capsys.readouterr() == (
        "",
        f"example agent: {name} must be positive seconds\n",
    )
    assert remote.records("ssh") == before


@pytest.mark.parametrize("grace", [10, 80])
def test_kill_grace_and_poll_bounds(
    remote: Remote, monkeypatch: pytest.MonkeyPatch, grace: int
) -> None:
    assert launch(remote) == 0
    config = dataclasses.replace(CONFIG, kill_grace_seconds=grace)
    run = driver.load(config, "sample")
    ticks = iter([0, 0, grace + 1, grace + 1, grace + 2])
    sleeps: list[float] = []
    monkeypatch.setattr(
        driver,
        "time",
        SimpleNamespace(monotonic=lambda: next(ticks), sleep=sleeps.append),
    )
    scripts: list[str] = []
    timeouts: list[float] = []

    def shell(settings: model.Settings, host: model.Host, script: str) -> str:
        assert settings == config
        assert host == run.host
        scripts.append(script)
        return ""

    def read(
        settings: model.Settings,
        host: model.Host,
        name: tuple[str, str],
        *,
        timeout: float,
    ) -> transport.Reading:
        assert settings == config
        assert host == run.host
        assert name == ("sample", driver.load(CONFIG, "sample").owner)
        timeouts.append(timeout)
        return transport.Reading(
            "stopping", None, launched=True, session=False, text=""
        )

    monkeypatch.setattr(transport, "shell", shell)
    monkeypatch.setattr(transport, "read", read)
    assert driver.kill(config, run) == 0
    assert timeouts == [min(30, grace + 2), 1]
    assert sleeps == [0.5]
    assert scripts == [
        remote_commands.session(
            "sample",
            "kill-session",
            CONFIG.sockets_dir,
            owner=driver.load(CONFIG, "sample").owner,
        ),
        remote_commands.fallback(
            "sample", CONFIG.sockets_dir, owner=driver.load(CONFIG, "sample").owner
        ),
    ]


def test_launch_poll_interval_and_budget(
    remote: Remote, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert launch(remote) == 0
    run = driver.load(CONFIG, "sample")
    ticks = iter([0, 1, 1, 2])
    sleeps: list[float] = []
    monkeypatch.setattr(
        driver,
        "time",
        SimpleNamespace(monotonic=lambda: next(ticks), sleep=sleeps.append),
    )
    outcomes = iter([False, True])
    timeouts: list[float] = []

    def read(
        settings: model.Settings,
        host: model.Host,
        name: tuple[str, str],
        *,
        timeout: float,
    ) -> transport.Reading:
        assert settings == CONFIG
        assert host == run.host
        assert name == ("sample", driver.load(CONFIG, "sample").owner)
        timeouts.append(timeout)
        return transport.Reading(
            "running", None, launched=next(outcomes), session=True, text=""
        )

    monkeypatch.setattr(transport, "read", read)
    assert driver.poll_launch(CONFIG, run, 120) == 0
    assert timeouts == [120, 119]
    assert sleeps == [0.5]


def test_metadata_permissions_under_an_open_mask(remote: Remote) -> None:
    args = agent.parser("example agent").parse_args(arguments(remote))
    run = driver.prepare(CONFIG, args)
    previous = os.umask(0)
    try:
        driver.save(run)
    finally:
        os.umask(previous)
    path = driver.metadata_path("sample")
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700
    loaded = driver.load(CONFIG, "sample")
    assert loaded.started == run.started
    assert loaded.request == run.request
    assert loaded.host == run.host
    assert loaded.name == run.name


def finish_run_files(remote: Remote) -> None:
    for suffix in runfiles_names.SUFFIXES:
        if suffix not in {".owner", ".rc.fallback"}:
            remote.file(suffix, "finished" if suffix == ".phase" else "0")


def test_clean_moves_every_run_file_with_one_utc_stamp(
    remote: Remote, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert launch(remote) == 0
    remote.configure(session=False)
    finish_run_files(remote)
    (remote.state / "index.jsonl").write_text("preserve\n")

    class FixedTime:
        @staticmethod
        def now(tz: dt.tzinfo) -> dt.datetime:
            assert tz is dt.UTC
            return dt.datetime(2026, 11, 9, 1, 2, 3, 456789, tzinfo=dt.UTC)

    monkeypatch.setattr(driver, "datetime", FixedTime)
    previous = os.umask(0)
    try:
        assert command("clean") == 0
    finally:
        os.umask(previous)
    stamp = "20261109T010203456789Z"
    local = remote.local / "tbhbdev/agent-runs/trash" / stamp
    assert local.stat().st_mode & 0o777 == 0o700
    assert [p.name for p in local.iterdir()] == ["sample.json"]
    trash = remote.state / "trash" / f"{stamp}-sample"
    assert (trash / "worktree").is_dir()
    assert sorted(p.name for p in trash.iterdir() if p.is_file()) == sorted(
        "sample" + suffix
        for suffix in runfiles_names.SUFFIXES
        if suffix != ".rc.fallback"
    )
    assert all(
        p.read_text() == ("finished" if p.suffix == ".phase" else "0")
        for p in trash.iterdir()
        if p.is_file() and p.suffix != ".owner"
    )
    assert (remote.state / "index.jsonl").read_text() == "preserve\n"


def test_exec_spends_one_startup_budget(
    remote: Remote, monkeypatch: pytest.MonkeyPatch
) -> None:
    steps: list[str] = []

    def shell(
        settings: model.Settings, host: model.Host, script: str, *, timeout: float
    ) -> str:
        assert settings == CONFIG
        assert host == CONFIG.hosts[0]
        assert script == remote_commands.installed(CONFIG.hosts[0])
        assert timeout == 110
        steps.append("preflight")
        return version.line() + "\n"

    def stream(
        settings: model.Settings,
        host: model.Host,
        name: str,
        *,
        inputs: transport.Inputs,
        timeout: float,
    ) -> None:
        assert settings == CONFIG
        assert host == CONFIG.hosts[0]
        assert name == "sample"
        assert timeout == 100
        assert inputs.owner == driver.load(CONFIG, "sample").owner
        assert inputs.schema is None
        assert inputs.prompt.endswith("Carry out the example.\n")
        assert json.loads(inputs.request)["commit"] is None
        steps.append("stream")

    def call(
        settings: model.Settings,
        host: model.Host,
        argv: tuple[str, ...],
        *,
        timeout: float,
    ) -> str:
        assert settings == CONFIG
        assert host == CONFIG.hosts[0]
        assert argv == remote_commands.start(
            "sample",
            CONFIG.sockets_dir,
            CONFIG.hosts[0],
            owner=driver.load(CONFIG, "sample").owner,
        )
        assert timeout == 90
        steps.append("start")
        return ""

    def read(
        settings: model.Settings,
        host: model.Host,
        name: tuple[str, str],
        *,
        timeout: float,
    ) -> transport.Reading:
        assert settings == CONFIG
        assert host == CONFIG.hosts[0]
        assert name == ("sample", driver.load(CONFIG, "sample").owner)
        assert timeout == 80
        steps.append("poll")
        return transport.Reading("running", None, launched=True, session=True, text="")

    monkeypatch.setattr(transport, "shell", shell)
    monkeypatch.setattr(transport, "stream", stream)
    monkeypatch.setattr(transport, "call", call)
    monkeypatch.setattr(transport, "read", read)
    assert launch(remote) == 0
    assert steps == ["preflight", "stream", "start", "poll"]


@pytest.mark.parametrize(
    ("flag", "value"),
    [("--harness", "bad"), ("--profile", "bad"), ("--timeout", "bad")],
)
def test_parser_rejects_invalid_choices_before_ssh(
    remote: Remote, flag: str, value: str
) -> None:
    assert launch(remote, flag, value) == 2
    assert remote.records("ssh") == []


def test_explicit_codex_harness(remote: Remote) -> None:
    assert launch(remote, "--harness", "codex") == 0


def test_review_version_mismatch_precedes_clone_read(
    remote: Remote, capsys: pytest.CaptureFixture[str]
) -> None:
    remote.configure(version="tbhbdev example-other")
    assert launch(remote, "--profile", "review", "--ref", "topic") == 1
    message = capsys.readouterr().err
    assert version.line() in message
    assert "tbhbdev example-other" in message
    assert remote.records("git") == []


def test_successful_wait_resets_failure_for_a_short_window(
    remote: Remote, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert launch(remote) == 0
    monkeypatch.setenv("TBHB_AGENT_WAIT_UNREACHABLE", "0.5")

    def read(
        _config: model.Settings,
        _host: model.Host,
        _name: tuple[str, str],
        *,
        answer: bool,
        timeout: float,
    ) -> transport.Reading:
        assert answer is True
        assert timeout == 0.5
        return transport.Reading("finished", 0, launched=True, session=False, text="")

    monkeypatch.setattr(transport, "read", read)
    assert command("wait") == 0


@pytest.mark.parametrize(
    "verb", ["status", "wait", "clean", "keep", "kill", "log", "diff"]
)
def test_verbs_without_remote_run_directory(
    remote: Remote,
    capsys: pytest.CaptureFixture[str],
    verb: str,
) -> None:
    assert launch(remote) == 0
    remote.configure(session=False)
    remote.state.rename(remote.root / "saved-run-files")
    capsys.readouterr()
    assert command(verb) == 1
    assert "not owner" in capsys.readouterr().err
    assert not driver.metadata_path("sample").exists()
    assert not remote.state.exists()


@pytest.mark.parametrize("finished", [False, True])
def test_clean_without_remote_clone(remote: Remote, finished: bool) -> None:
    assert launch(remote) == 0
    remote.configure(session=False)
    clone = remote.root / "home/Code/github.com/example-org/example-repo"
    clone.rename(remote.root / "saved-clone")
    if finished:
        remote.file(".rc", "0")
    options = () if finished else ("--force",)
    assert command("clean", *options) == 0
    assert not driver.metadata_path("sample").exists()
    assert len(remote.records("git")) == (1 if finished else 0)


@pytest.mark.parametrize("existing", ["session", ".rc", ".launched"])
def test_remote_name_collision_preserves_other_run(
    remote: Remote, capsys: pytest.CaptureFixture[str], existing: str
) -> None:
    if existing == "session":
        remote.configure(session=True)
    else:
        remote.file(existing, "other run\n")
    before = {p.name: p.read_bytes() for p in remote.state.iterdir()}
    assert launch(remote) == 1
    assert "collision" in capsys.readouterr().err
    assert {p.name: p.read_bytes() for p in remote.state.iterdir()} == before
    assert not any("kill-session" in str(r) for r in remote.records("tmux"))
    assert not driver.metadata_path("sample").exists()


@pytest.mark.parametrize(
    "record", ["{", "[]", "{}", '{"host":"example-linux"}', "0", "null"]
)
def test_malformed_metadata_names_file(
    remote: Remote, capsys: pytest.CaptureFixture[str], record: str
) -> None:
    path = driver.metadata_path("sample")
    assert path.is_relative_to(remote.local)
    path.parent.mkdir(parents=True)
    path.write_text(record)
    assert command("status") == 1
    assert str(path) in capsys.readouterr().err


def test_existing_metadata_directory_permissions(remote: Remote) -> None:
    path = driver.metadata_path("sample")
    path.parent.mkdir(parents=True)
    path.parent.chmod(0o755)
    assert launch(remote) == 0
    assert path.parent.stat().st_mode & 0o777 == 0o700


def test_poll_transport_failure_only_kills_created_session(
    remote: Remote, capsys: pytest.CaptureFixture[str]
) -> None:
    remote.configure(ssh=[None, None, None, {"rc": 255}], kill_rc="1\n")
    assert launch(remote) == 1
    assert capsys.readouterr().out == (
        "sample: launch unconfirmed host=example-linux phase=unreachable\n"
    )
    assert any("kill-session" in str(r) for r in remote.records("tmux"))


def test_wait_tmux_error_expires_without_rc(
    remote: Remote, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert launch(remote) == 0
    remote.configure(tmux_error="unrelated failure")
    ticking(monkeypatch, 240)
    assert command("wait") == 75
    assert not (remote.state / "sample.rc").exists()


def test_load_hardens_existing_metadata_directory(remote: Remote) -> None:
    assert launch(remote) == 0
    path = driver.metadata_path("sample")
    path.parent.chmod(0o755)
    assert command("status") == 0
    assert path.parent.stat().st_mode & 0o777 == 0o700


def test_metadata_directory_is_private_before_chmod(
    remote: Remote, monkeypatch: pytest.MonkeyPatch
) -> None:
    args = agent.parser("example agent").parse_args(arguments(remote))
    run = driver.prepare(CONFIG, args)
    directory = driver.metadata_path("sample").parent
    original = type(directory).chmod

    def chmod(path: Path, mode: int) -> None:
        assert path == directory
        assert path.stat().st_mode & 0o777 == 0o700
        original(path, mode)

    monkeypatch.setattr(type(directory), "chmod", chmod)
    previous = os.umask(0)
    try:
        driver.save(run)
    finally:
        os.umask(previous)


def test_clean_allows_name_reuse(remote: Remote) -> None:
    assert launch(remote) == 0
    remote.file(".run.lock", "")
    remote.file(".rc", "0\n")
    remote.configure(session=False)
    assert command("clean") == 0
    assert launch(remote) == 0


def test_keep_twice(remote: Remote) -> None:
    assert launch(remote) == 0
    assert command("keep") == 0
    assert command("keep") == 0


def test_kill_ignores_stale_pending(
    remote: Remote, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert launch(remote) == 0
    remote.file(".rc.pending", "old partial result")
    ticking(monkeypatch)
    assert command("kill") == 0
    assert (remote.state / "sample.rc").read_text() == "137\n"
    assert (remote.state / "sample.rc.fallback").is_file()


def test_clean_fallback_requires_force(
    remote: Remote, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert launch(remote) == 0
    ticking(monkeypatch)
    assert command("kill") == 0
    assert command("clean") == 1
    assert command("clean", "--force") == 0


@pytest.mark.parametrize(
    "verb", ["kill", "wait", "clean", "status", "keep", "log", "diff"]
)
def test_failed_transfer_cannot_control_other_owner(
    remote: Remote,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    verb: str,
) -> None:
    def fail(*_args: object, **_kwargs: object) -> None:
        remote.file(".owner", "another-owner\n")
        remote.file(".rc", "0\n")
        message = "transfer failed"
        raise transport.TransportError(message)

    monkeypatch.setattr(transport, "stream", fail)
    assert launch(remote) == 1
    assert not driver.metadata_path("sample").exists()
    archived = next((driver.directory() / "trash").glob("*/sample.json"))
    driver.metadata_path("sample").write_bytes(archived.read_bytes())
    before = {p.name: p.read_bytes() for p in remote.state.iterdir()}
    capsys.readouterr()
    assert command(verb) == 1
    assert "not owner" in capsys.readouterr().err
    assert not driver.metadata_path("sample").exists()
    assert {p.name: p.read_bytes() for p in remote.state.iterdir()} == before
    assert not remote.records("tmux")


def test_lost_clean_reply_cannot_clean_new_owner(
    remote: Remote,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert launch(remote) == 0
    remote.configure(session=False)
    remote.file(".rc", "0\n")
    original = transport.shell

    def lose(
        config: model.Settings,
        host: model.Host,
        script: str,
        *,
        timeout: float = 30,
        data: bytes = b"",
    ) -> str:
        result = original(config, host, script, timeout=timeout, data=data)
        if 'trash="$d/trash/' in script:
            remote.file(".owner", "new-owner\n")
            remote.file(".rc", "0\n")
            message = "lost clean reply"
            raise transport.TransportError(message)
        return result

    monkeypatch.setattr(transport, "shell", lose)
    assert command("clean") == 1
    monkeypatch.setattr(transport, "shell", original)
    before = {p.name: p.read_bytes() for p in remote.state.iterdir() if p.is_file()}
    assert command("clean") == 1
    assert "not owner" in capsys.readouterr().err
    assert not driver.metadata_path("sample").exists()
    assert {
        p.name: p.read_bytes() for p in remote.state.iterdir() if p.is_file()
    } == before


@pytest.mark.parametrize("transferred", [True, False])
def test_transfer_error_checks_owner_before_retaining_metadata(
    remote: Remote,
    monkeypatch: pytest.MonkeyPatch,
    transferred: bool,
    capsys: pytest.CaptureFixture[str],
) -> None:
    original = transport.stream

    def fail(
        config: model.Settings,
        host: model.Host,
        name: str,
        *,
        inputs: transport.Inputs,
        timeout: float,
    ) -> None:
        if transferred:
            original(config, host, name, inputs=inputs, timeout=timeout)
        else:
            remote.configure(ssh=[{"rc": 255}])
        message = "lost transfer reply"
        raise transport.TransportError(message)

    monkeypatch.setattr(transport, "stream", fail)
    assert launch(remote) == 75
    assert driver.metadata_path("sample").exists()
    assert capsys.readouterr() == (
        "sample: launch unconfirmed host=example-linux phase=unreachable\n",
        "",
    )
    assert not any("kill-session" in str(r) for r in remote.records("tmux"))


def test_lost_start_reply_keeps_unreachable_run_reachable(
    remote: Remote,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    original = transport.call

    def lost_reply(
        config: model.Settings,
        host: model.Host,
        command: tuple[str, ...],
        *,
        data: bytes = b"",
        timeout: float = 30,
    ) -> str:
        reply = original(config, host, command, data=data, timeout=timeout)
        if "new-session" in command[2]:
            remote.configure(ssh=[{"rc": 255}])
            message = "lost start reply"
            raise transport.TransportError(message)
        return reply

    with monkeypatch.context() as patch:
        patch.setattr(transport, "call", lost_reply)
        result = launch(remote)
    assert driver.metadata_path("sample").exists()
    assert result == 75
    assert capsys.readouterr() == (
        "sample: launch unconfirmed host=example-linux phase=unreachable\n",
        "",
    )
    run = driver.load(CONFIG, "sample")
    assert (remote.state / "sample.owner").read_text() == run.owner + "\n"
    assert not any("kill-session" in str(r) for r in remote.records("tmux"))
    assert command("status") == 0
    assert "phase=unknown" in capsys.readouterr().out
    remote.file(".rc", "7\n")
    remote.configure(session=False)
    assert command("wait") == 7


def test_owner_lost_during_launch_poll(
    remote: Remote,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def read(*_args: object, **_kwargs: object) -> transport.Reading:
        message = "owner lost"
        raise transport.CollisionError(message)

    monkeypatch.setattr(transport, "read", read)
    assert launch(remote) == 1
    assert "not owner" in capsys.readouterr().err
    assert not driver.metadata_path("sample").exists()


def test_clean_retry_after_rc_was_trashed(
    remote: Remote,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert launch(remote) == 0
    remote.configure(session=False)
    remote.file(".rc", "0\n")
    original = transport.shell

    def fail(
        config: model.Settings,
        host: model.Host,
        script: str,
        *,
        timeout: float = 30,
        data: bytes = b"",
    ) -> str:
        if 'trash="$d/trash/' in script:
            (remote.state / "sample.rc").rename(remote.root / "saved-rc")
            message = "failed before owner move"
            raise transport.TransportError(message)
        return original(config, host, script, timeout=timeout, data=data)

    monkeypatch.setattr(transport, "shell", fail)
    assert command("clean") == 1
    monkeypatch.setattr(transport, "shell", original)
    assert command("clean") == 0


def test_owner_lost_during_unconfirmed_shutdown(
    remote: Remote,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def fail(*_args: object, **_kwargs: object) -> transport.Reading:
        remote.file(".owner", "new-owner\n")
        message = "lost poll reply"
        raise transport.TransportError(message)

    monkeypatch.setattr(transport, "read", fail)
    assert launch(remote) == 1
    assert "not owner" in capsys.readouterr().err
    assert not driver.metadata_path("sample").exists()
    assert not any("kill-session" in str(r) for r in remote.records("tmux"))


def test_not_owner_archives_with_utc_stamp(remote: Remote) -> None:
    assert launch(remote) == 0
    remote.file(".owner", "new-owner\n")
    assert command("status") == 1
    archived = next((driver.directory() / "trash").glob("*/sample.json"))
    stamp = dt.datetime.fromisoformat(archived.parent.name)
    assert stamp.utcoffset() == dt.timedelta(0)


@pytest.mark.parametrize("verb", ["log", "diff"])
def test_inspection_uses_recorded_target_and_mirror(
    remote: Remote, capsys: pytest.CaptureFixture[str], verb: str
) -> None:
    assert launch(remote) == 0
    capsys.readouterr()
    assert command(verb) == 0
    assert capsys.readouterr() == (verb + " output\n", "")
    calls = [record["args"] for record in remote.records("git")]
    assert len(calls) == 2
    assert isinstance(calls[0], list)
    assert isinstance(calls[1], list)
    assert "fetch" in calls[0]
    assert verb in calls[1]
    assert "--no-optional-locks" in calls[1]
    if verb == "log":
        assert calls[1][calls[1].index("-n") + 1] == "10"
    else:
        assert "-n" not in calls[1]
    assert len(remote.records("ssh")) == 5

    mirror_path = (
        remote.root
        / "state/tbhbdev/agent-mirrors/github.com/example-org/example-repo.git"
    )
    mirror_path.rename(remote.root / "unused-mirror")
    mirror = mirror_in(remote.root)
    clone = (
        remote.root
        / "home/Code/github.com/example-org/example-repo/.worktrees/agent-sample"
    )
    git_in(clone, "init")
    marker = remote.root / "clone-origin-executed"
    git_in(clone, "remote", "add", "origin", f"ext::touch {marker}")
    write_standalone_fake(
        remote.bin / "git",
        "import os, sys\nos.execv('/usr/bin/git', ['/usr/bin/git', *sys.argv[1:]])\n",
    )
    assert command(verb) == 0
    output = capsys.readouterr()
    assert "pushed change" in output.out
    assert output.err == ""
    assert not marker.exists()
    assert git_in(mirror, "rev-parse", "refs/heads/agent/sample") == git_in(
        remote.root / "source", "rev-parse", "agent/sample"
    )
    git_in(mirror, "config", "remote.origin.url", str(remote.root / "missing.git"))
    assert command(verb) == 1


def test_clean_rejects_suffix_matching_origin_branch(
    remote: Remote, capsys: pytest.CaptureFixture[str]
) -> None:
    assert launch(remote) == 0
    remote.configure(session=False)
    remote.file(".rc", "0")
    mirror_path = (
        remote.root
        / "state/tbhbdev/agent-mirrors/github.com/example-org/example-repo.git"
    )
    mirror_path.rename(remote.root / "unused-mirror")
    mirror_in(remote.root)
    origin = remote.root / "origin.git"
    commit = git_in(origin, "rev-parse", "refs/heads/agent/sample")
    git_in(origin, "update-ref", "refs/heads/x/refs/heads/agent/sample", commit)
    git_in(origin, "update-ref", "-d", "refs/heads/agent/sample")
    write_standalone_fake(
        remote.bin / "git",
        "import os, sys\nos.execv('/usr/bin/git', ['/usr/bin/git', *sys.argv[1:]])\n",
    )
    assert command("clean") == 1
    assert "does not exist on origin" in capsys.readouterr().err
    assert driver.metadata_path("sample").exists()
    assert (remote.state / "sample.owner").exists()


@pytest.mark.parametrize("verb", ["log", "diff"])
def test_inspection_filters_terminal_controls(
    remote: Remote, capsys: pytest.CaptureFixture[str], verb: str
) -> None:
    assert launch(remote) == 0
    capsys.readouterr()
    remote.configure(**{verb: "é\tline\n\x1b\x07\x00\x7f\x85\u200e"})
    assert command(verb) == 0
    assert capsys.readouterr() == ("é\tline\n", "")


def test_inspection_warning(remote: Remote, capsys: pytest.CaptureFixture[str]) -> None:
    assert launch(remote) == 0
    capsys.readouterr()
    remote.configure(session=False)
    remote.file(".rc", "0")
    assert command("wait") == 0
    assert (
        "Don't run git inside a run clone: it is agent-written. "
        "Use `tbhbdev alpha agent log|diff NAME`." in capsys.readouterr().out
    )


def test_log_count(remote: Remote) -> None:
    assert launch(remote) == 0
    assert command("log", "-n", "2") == 0
    args = remote.records("git")[-1]["args"]
    assert isinstance(args, list)
    assert args[args.index("-n") + 1] == "2"


@pytest.mark.parametrize("verb", ["log", "diff"])
def test_inspection_fetch_failure_stops_before_read(remote: Remote, verb: str) -> None:
    assert launch(remote) == 0
    remote.configure(fetch_error=74)
    assert command(verb) == 1
    assert len(remote.records("git")) == 1


@pytest.mark.parametrize(
    "args",
    [
        ["diff", "sample", "-n", "1"],
        ["log", "sample", "--patch"],
        ["log", "sample", "-n", "invalid"],
    ],
)
def test_inspection_rejects_extra_options(args: list[str]) -> None:
    assert agent.main(args, "example agent") == 2


def test_unsandboxed_force_clean_refuses_live_session(
    remote: Remote, capsys: pytest.CaptureFixture[str]
) -> None:
    assert launch(remote, "--ops", "--unsandboxed", "o/r#1") == 0
    before = remote.records("git")
    assert command("clean", "--force") == 1
    assert "run is running; clean refused" in capsys.readouterr().err
    assert driver.metadata_path("sample").exists()
    assert (remote.state / "sample.owner").is_file()
    assert not (remote.state / "trash").exists()
    assert remote.records("git") == before


@pytest.mark.parametrize("verb", ["status", "kill", "clean", "log", "diff"])
def test_unsandboxed_verbs_after_host_setting_disabled(
    remote: Remote,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    verb: str,
) -> None:
    assert launch(remote, "--ops", "--unsandboxed", "o/r#1") == 0
    disabled = dataclasses.replace(
        CONFIG,
        hosts=tuple(
            dataclasses.replace(host, unsandboxed=False) for host in CONFIG.hosts
        ),
    )
    monkeypatch.setattr(agent.settings, "load", lambda: disabled)
    capsys.readouterr()
    if verb in {"kill", "clean"}:
        remote.configure(session=False)
        remote.file(".rc", "0")
    before = remote.records("ssh")
    assert command(verb, *(["--force"] if verb == "clean" else [])) == (
        65 if verb in {"log", "diff"} else 0
    )
    output = capsys.readouterr()
    if verb == "status":
        assert "sandbox=none authorized_by=o/r#1" in output.out
    if verb in {"log", "diff"}:
        assert "unsandboxed run: inspect agent/sample on GitHub" in output.err
        assert remote.records("ssh") == before
    if verb == "clean":
        assert not driver.metadata_path("sample").exists()
    if verb == "kill":
        assert "kill-session" in str(remote.records("tmux")[-1])


def test_unsandboxed_new_launch_after_host_setting_disabled(
    remote: Remote,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    disabled = dataclasses.replace(
        CONFIG,
        hosts=tuple(
            dataclasses.replace(host, unsandboxed=False) for host in CONFIG.hosts
        ),
    )
    monkeypatch.setattr(agent.settings, "load", lambda: disabled)
    assert launch(remote, "--ops", "--unsandboxed", "o/r#1") == 1
    assert "isn't enabled on this host" in capsys.readouterr().err
    assert remote.records("ssh") == []

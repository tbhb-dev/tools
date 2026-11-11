"""Local host trust gates every driver launch before remote preparation."""

from __future__ import annotations

import json
from datetime import datetime
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest
from agent_driver_example import Remote, execution_arguments, setup
from agent_example import settings

from tbhbdev.alpha import agent
from tbhbdev.alpha.agent import driver, taint_store

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def remote(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Remote:
    result = setup(tmp_path, monkeypatch)
    monkeypatch.setattr(agent.settings, "load", settings)
    return result


def launch(remote: Remote, *options: str) -> int:
    return agent.main(execution_arguments(remote.prompt, *options), "example agent")


def clear(host: str = "example-linux", reason: str = "Host rebuilt") -> int:
    return agent.main(["clear-taint", host, "--reason", reason], "example agent")


def test_unknown_review_refused_before_ssh(
    remote: Remote, capsys: pytest.CaptureFixture[str]
) -> None:
    assert launch(remote, "--profile", "review", "--ref", "a" * 40) == 65
    assert remote.records("ssh") == []
    output = capsys.readouterr()
    assert output.out == ""
    assert "example-linux" in output.err
    assert "unknown" in output.err
    assert "clear-taint example-linux --reason" in output.err


def test_unknown_build_label_and_required_request(
    remote: Remote, capsys: pytest.CaptureFixture[str]
) -> None:
    assert launch(remote) == 0
    assert "untrusted-host" in capsys.readouterr().out
    wire = json.loads((remote.state / "sample.request.json").read_text())
    assert wire["v"] == 2
    assert wire["tainted_host"] is True
    assert agent.main(["status", "sample"], "example agent") == 0
    assert "host=unknown" in capsys.readouterr().out


def test_clear_host_and_launch_review(remote: Remote) -> None:
    assert clear() == 0
    remote.configure(refs="a" * 40 + "\tHEAD\n")
    assert launch(remote, "--profile", "review") == 0
    assert taint_store.load("example-linux").status == "clean"
    calls = remote.records("ssh")
    assert "tbhbdev-agent-taint/agent.jsonl" in str(calls[0])
    assert "--version" in str(calls[1])
    wire = json.loads((remote.state / "sample.request.json").read_text())
    assert wire["tainted_host"] is False
    assert wire["v"] == 2


def test_unsandboxed_append_precedes_preparation(
    remote: Remote, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert clear() == 0
    original = driver.transport.call

    def call(
        config: agent.driver.Settings,
        host: agent.driver.Host,
        command: tuple[str, ...],
        *,
        data: bytes = b"",
        timeout: float = 30,
    ) -> str:
        if "tbhbdev-agent-taint/ops.jsonl" not in command[2]:
            state = taint_store.load("example-linux")
            assert state.status == "tainted"
            assert state.taint is not None
            assert state.taint.account == "ops"
            assert state.taint.run == "sample"
            assert state.taint.authorized_by == "o/r#1"
        return original(config, host, command, data=data, timeout=timeout)

    monkeypatch.setattr(driver.transport, "call", call)
    assert launch(remote, "--ops", "--unsandboxed", "o/r#1") == 0
    record = json.loads(driver.metadata_path("sample").read_text())
    saved = taint_store.load("example-linux").taint
    assert saved is not None
    assert saved.started == record["started"]
    assert record["tainted_host"] is True
    wire = json.loads((remote.state / "sample.request.json").read_text())
    assert wire["tainted_host"] is True
    assert agent.main(["status", "sample"], "example agent") == 0


def test_tainted_host_refuses_other_account_before_ssh(
    remote: Remote, capsys: pytest.CaptureFixture[str]
) -> None:
    assert launch(remote, "--ops", "--unsandboxed", "o/r#1") == 0
    driver.metadata_path("sample").rename(driver.directory() / "saved.json")
    before = remote.records("ssh")
    assert (
        launch(remote, "--account", "agent", "--profile", "review", "--ref", "a" * 40)
        == 65
    )
    assert remote.records("ssh") == before
    assert "example-linux host=tainted" in capsys.readouterr().err
    assert (
        agent.main(
            ["clear-taint", "example-linux", "ops", "--reason", "Rebuilt"],
            "example agent",
        )
        == 2
    )


def test_append_failure_refuses_launch(
    remote: Remote, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def fail(_record: object) -> None:
        message = "incomplete taint store append"
        raise OSError(message)

    monkeypatch.setattr(taint_store, "record_taint", fail)
    assert launch(remote, "--ops", "--unsandboxed", "o/r#1") == 1
    assert "incomplete taint store append" in capsys.readouterr().err
    assert remote.records("ssh") == []
    assert not driver.metadata_path("sample").exists()


@pytest.mark.parametrize("marker", ["\n", '{"untrusted":"bytes"}\n', "x" * 70000])
@pytest.mark.parametrize("profile", ["review", "build"])
def test_host_marker_is_evidence_only(
    remote: Remote, capsys: pytest.CaptureFixture[str], marker: str, profile: str
) -> None:
    assert clear() == 0
    path = remote.root / "markers/agent.jsonl"
    path.write_text(marker)
    capsys.readouterr()
    assert launch(
        remote,
        "--profile",
        profile,
        *(["--ref", "a" * 40] if profile == "review" else []),
    ) == (65 if profile == "review" else 0)
    calls = remote.records("ssh")
    assert len(calls) == (1 if profile == "review" else 5)
    loaded = taint_store.load("example-linux")
    assert loaded.status == "tainted"
    assert loaded.taint is not None
    assert loaded.taint.run == "host-marker"
    assert loaded.taint.authorized_by == "host-marker"
    assert loaded.taint.account == "agent"
    if profile == "review":
        assert capsys.readouterr().err == (
            "review runs refused: example-linux host=tainted; "
            "clear with: tbhbdev alpha agent clear-taint example-linux --reason …\n"
        )
    else:
        assert "untrusted-host" in capsys.readouterr().out


@pytest.mark.parametrize("profile", ["review", "build"])
def test_host_marker_read_error_refuses_review(
    remote: Remote, capsys: pytest.CaptureFixture[str], profile: str
) -> None:
    assert clear() == 0
    # A directory cannot be read as a marker, on either supported host OS.
    (remote.root / "markers/agent.jsonl").mkdir()
    assert launch(
        remote,
        "--profile",
        profile,
        *(["--ref", "a" * 40] if profile == "review" else []),
    ) == (65 if profile == "review" else 0)
    assert len(remote.records("ssh")) == (1 if profile == "review" else 5)
    output = capsys.readouterr()
    if profile == "review":
        assert output.err == (
            "review runs refused: example-linux host=tainted; "
            "clear with: tbhbdev alpha agent clear-taint example-linux --reason …\n"
        )
    else:
        assert "untrusted-host" in output.out


def test_marker_read_is_bounded(remote: Remote) -> None:
    (remote.root / "markers/agent.jsonl").write_bytes(b"x" * 70000)
    value = driver.transport.shell(
        settings(), settings().hosts[0], driver.remote.host_marker("agent")
    )
    assert len(value) == 65536
    assert len(remote.records("ssh")) == 1


def test_clearance_prints_state_and_repairs_partial_line(
    remote: Remote, capsys: pytest.CaptureFixture[str]
) -> None:
    assert clear() == 0
    assert capsys.readouterr().out == "example-linux: host=clean\n"
    path = driver.directory() / "taint/example-linux.jsonl"
    path.write_text('{"kind":')
    assert launch(remote, "--profile", "review", "--ref", "a" * 40) == 65
    assert remote.records("ssh") == []
    assert clear(reason="Host rebuilt again") == 0
    assert taint_store.load("example-linux").status == "clean"
    assert list(path.parent.glob("example-linux.jsonl.broken-*"))
    assert json.loads(path.read_text())["reason"] == "Host rebuilt again"
    assert launch(remote, "--profile", "review", "--ref", "a" * 40) == 0


def test_clearance_between_prepare_and_append_does_not_hide_taint(
    remote: Remote, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = taint_store.record_taint

    def delayed(record: driver.taint.Taint) -> None:
        assert clear(reason="Host cleared after run preparation") == 0
        original(record)

    monkeypatch.setattr(taint_store, "record_taint", delayed)
    assert launch(remote, "--ops", "--unsandboxed", "o/r#1") == 0
    assert taint_store.load("example-linux").status == "tainted"


def test_driver_clearance_survives_clock_step_back(
    remote: Remote, monkeypatch: pytest.MonkeyPatch
) -> None:
    times = iter(
        [
            datetime.fromisoformat("2026-11-10T13:00:00Z"),
            datetime.fromisoformat("2026-11-10T12:00:00Z"),
        ]
    )

    def now(_zone: object) -> datetime:
        return next(times)

    monkeypatch.setattr(driver, "datetime", SimpleNamespace(now=now))
    assert launch(remote, "--ops", "--unsandboxed", "o/r#1") == 0
    assert clear(reason="Host rebuilt after the clock moved back") == 0
    assert taint_store.load("example-linux").status == "clean"


def test_unknown_build_that_finishes_during_launch_is_labelled(
    remote: Remote, capsys: pytest.CaptureFixture[str]
) -> None:
    remote.configure(launch={"rc": "0"})
    assert launch(remote) == 0
    assert capsys.readouterr() == ("sample: rc=0 untrusted-host\n", "")


@pytest.mark.parametrize("cleared", [False, True])
def test_prepare_reads_local_host_trust(remote: Remote, *, cleared: bool) -> None:
    if cleared:
        assert clear() == 0
    args = agent.parser("example agent").parse_args(execution_arguments(remote.prompt))
    run = driver.prepare(settings(), args)
    assert run.request.tainted_host is (not cleared)
    assert remote.records("ssh") == []


@pytest.mark.parametrize(
    "options", [[], ["example-linux"], ["--reason", "Host rebuilt"]]
)
def test_clear_taint_requires_host_and_reason(
    remote: Remote, options: list[str]
) -> None:
    assert agent.main(["clear-taint", *options], "example agent") == 2
    assert remote.records("ssh") == []
    assert not (driver.directory() / "taint").exists()


@pytest.mark.parametrize("account", ["agent", "ops"])
def test_empty_marker_for_run_account_allows_review(
    remote: Remote, account: str
) -> None:
    assert clear() == 0
    (remote.root / "markers" / f"{account}.jsonl").write_text("")
    other = "ops" if account == "agent" else "agent"
    (remote.root / "markers" / f"{other}.jsonl").write_text("marked\n")
    assert (
        launch(remote, "--account", account, "--profile", "review", "--ref", "a" * 40)
        == 0
    )
    assert len(remote.records("ssh")) == 5
    assert taint_store.load("example-linux").status == "clean"


@pytest.mark.parametrize("cleared", [False, True])
@pytest.mark.parametrize("unsandboxed", [False, True])
def test_prepare_alone_fails_closed(
    remote: Remote, *, cleared: bool, unsandboxed: bool
) -> None:
    if cleared:
        assert clear() == 0
    options = ["--ops", "--unsandboxed", "o/r#1"] if unsandboxed else []
    args = agent.parser("example agent").parse_args(
        execution_arguments(remote.prompt, *options)
    )
    run = driver.prepare(settings(), args)
    assert run.request.tainted_host is (unsandboxed or not cleared)
    assert taint_store.load("example-linux").status == (
        "clean" if cleared else "unknown"
    )
    assert remote.records("ssh") == []

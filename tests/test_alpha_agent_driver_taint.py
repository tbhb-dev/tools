"""Local host trust gates every driver launch before remote preparation."""

from __future__ import annotations

import json
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
    assert "agent-taint.jsonl" in str(calls[0])
    assert "--version" in str(calls[1])
    wire = json.loads((remote.state / "sample.request.json").read_text())
    assert wire["tainted_host"] is False
    assert wire["v"] == 2


def test_unsandboxed_append_precedes_every_remote_action(
    remote: Remote, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = driver.transport.call

    def call(
        config: agent.driver.Settings,
        host: agent.driver.Host,
        command: tuple[str, ...],
        *,
        data: bytes = b"",
        timeout: float = 30,
    ) -> str:
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
    path = remote.state.parent / "agent-taint.jsonl"
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
        assert "clear-taint example-linux --reason" in capsys.readouterr().err
    else:
        assert "untrusted-host" in capsys.readouterr().out


@pytest.mark.parametrize("profile", ["review", "build"])
def test_host_marker_read_error_refuses_review(
    remote: Remote, capsys: pytest.CaptureFixture[str], profile: str
) -> None:
    assert clear() == 0
    # A directory cannot be read as a marker, on either supported host OS.
    (remote.state.parent / "agent-taint.jsonl").mkdir()
    assert launch(
        remote,
        "--profile",
        profile,
        *(["--ref", "a" * 40] if profile == "review" else []),
    ) == (65 if profile == "review" else 0)
    assert len(remote.records("ssh")) == (1 if profile == "review" else 5)
    output = capsys.readouterr()
    assert (
        ("clear-taint" in output.err)
        if profile == "review"
        else ("untrusted-host" in output.out)
    )


def test_marker_read_is_bounded(remote: Remote) -> None:
    (remote.state.parent / "agent-taint.jsonl").write_bytes(b"x" * 70000)
    value = driver.transport.shell(
        settings(), settings().hosts[0], driver.remote.host_marker()
    )
    assert len(value) == 65536


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

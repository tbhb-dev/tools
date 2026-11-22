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
from tbhbdev.alpha.agent import driver, taint_store, transport

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.usefixtures("isolated_home")  # noqa: V107


@pytest.fixture
def remote(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Remote:
    result = setup(tmp_path, monkeypatch)
    monkeypatch.setattr(agent.settings, "load", settings)
    return result


def launch(remote: Remote, *options: str) -> int:
    return agent.main(execution_arguments(remote.prompt, *options), "example agent")


def clear(host: str = "example-linux", reason: str = "Host rebuilt") -> int:
    return agent.main(["clear-taint", host, "--reason", reason], "example agent")


@pytest.mark.parametrize("state", ["unknown", "tainted", "clean", "broken"])
@pytest.mark.parametrize("profile", ["build", "review"])
@pytest.mark.parametrize("harness", ["codex", "claude", "agy"])
@pytest.mark.parametrize("authorization", [None, "o/r#1"])
def test_default_launch_ignores_host_taint_and_records_full_access(  # noqa: PLR0913, PLR0917 -- launch matrix
    remote: Remote,
    capsys: pytest.CaptureFixture[str],
    state: str,
    profile: str,
    harness: str,
    authorization: str | None,
) -> None:
    if state == "clean":
        assert clear() == 0
    elif state == "tainted":
        taint_store.record_taint(
            driver.taint.Taint(
                "example-linux", "ops", "previous", "o/r#2", "2026-11-15T00:00:00Z"
            )
        )
    elif state == "broken":
        path = driver.directory() / "taint/example-linux.jsonl"
        path.parent.mkdir(parents=True, mode=0o700)
        path.parent.parent.chmod(0o700)
        path.write_text("broken\n")
        path.chmod(0o600)
    options = ["--profile", profile, "--harness", harness]
    if profile == "review":
        options += ["--ref", "a" * 40]
    if harness == "claude":
        options += ["--reason", "test run"]
    if harness == "agy":
        options += ["--model", "gemini-test"]
    if authorization is not None:
        options += ["--unsandboxed", authorization]
    capsys.readouterr()
    assert launch(remote, *options) == 0
    wire = json.loads((remote.state / "sample.request.json").read_text())
    assert wire["unsandboxed"] is True
    assert wire["tainted_host"] is True
    assert wire["authorized_by"] == authorization
    saved = taint_store.load("example-linux")
    assert saved.status == "tainted"
    recorded = json.loads(
        (driver.directory() / "taint/example-linux.jsonl").read_text().splitlines()[-1]
    )
    assert recorded["authorized_by"] == (authorization or "agent-exec-default")
    assert recorded["run"] == "sample"
    assert recorded["account"] == "agent"
    assert "untrusted-host" in capsys.readouterr().out
    assert len(remote.records("ssh")) == 4


def test_unknown_build_label_and_required_request(
    remote: Remote, capsys: pytest.CaptureFixture[str]
) -> None:
    assert launch(remote) == 0
    assert "untrusted-host" in capsys.readouterr().out
    wire = json.loads((remote.state / "sample.request.json").read_text())
    assert wire["v"] == 2
    assert wire["tainted_host"] is True
    assert agent.main(["status", "sample"], "example agent") == 0
    assert "host=tainted" in capsys.readouterr().out


def test_clear_host_and_launch_review(remote: Remote) -> None:
    assert clear() == 0
    remote.configure(refs="a" * 40 + "\tHEAD\n")
    assert launch(remote, "--profile", "review", "--ref", "HEAD") == 0
    assert taint_store.load("example-linux").status == "tainted"
    calls = remote.records("ssh")
    assert "--version" in str(calls[0])
    wire = json.loads((remote.state / "sample.request.json").read_text())
    assert wire["tainted_host"] is True
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
        stdin: transport.Stdin | None = None,
        *,
        timeout: float = 30,
    ) -> str:
        if (
            "tbhbdev-agent-taint/ops.jsonl" not in command[2]
            and "# tbhbdev-limits" not in command[2]
        ):
            state = taint_store.load("example-linux")
            assert state.status == "tainted"
            assert state.taint is not None
            assert state.taint.account == "ops"
            assert state.taint.run == "sample"
            assert state.taint.authorized_by == "o/r#1"
        return original(config, host, command, stdin, timeout=timeout)

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


@pytest.mark.parametrize("marker", ["", "marked", "directory"])
@pytest.mark.parametrize("profile", ["review", "build"])
def test_legacy_host_marker_never_refuses_for_taint(
    remote: Remote, marker: str, profile: str
) -> None:
    assert clear() == 0
    path = remote.root / "markers/agent.jsonl"
    if marker == "directory":
        path.mkdir()
    else:
        path.write_text(marker)
    args = agent.parser("example agent").parse_args(execution_arguments(remote.prompt))
    run = driver.prepare(settings(), args)
    run.request = driver.replace(
        run.request, unsandboxed=False, profile=agent.driver.Profile(profile)
    )
    assert driver.launch_trust(settings(), run, driver.time.monotonic() + 30) == 0
    assert taint_store.load("example-linux").status == (
        "tainted" if marker else "clean"
    )
    assert run.request.tainted_host is bool(marker)


def test_legacy_marker_transport_failure_keeps_transport_refusal(
    remote: Remote, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert clear() == 0
    capsys.readouterr()
    args = agent.parser("example agent").parse_args(execution_arguments(remote.prompt))
    run = driver.prepare(settings(), args)
    run.request = driver.replace(run.request, unsandboxed=False)

    def fail(*_args: object, **_kwargs: object) -> str:
        message = "ssh failed"
        raise driver.transport.TransportError(message)

    monkeypatch.setattr(driver.transport, "shell", fail)
    assert driver.launch_trust(settings(), run, driver.time.monotonic() + 30) == 66
    assert capsys.readouterr().err == (
        "cannot read marker (transport): example-linux not reachable over ssh\n"
    )
    assert taint_store.load("example-linux").status == "clean"


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
    assert run.request.tainted_host is True
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
    assert len(remote.records("ssh")) == 4
    assert taint_store.load("example-linux").status == "tainted"


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
    assert run.request.tainted_host is True
    assert taint_store.load("example-linux").status == (
        "clean" if cleared else "unknown"
    )
    assert remote.records("ssh") == []


def test_unsandboxed_review_launches_on_unknown_host(remote: Remote) -> None:
    assert (
        launch(
            remote, "--profile", "review", "--ref", "a" * 40, "--unsandboxed", "o/r#1"
        )
        == 0
    )
    wire = json.loads((remote.state / "sample.request.json").read_text())
    assert wire["unsandboxed"] is True
    assert wire["tainted_host"] is True

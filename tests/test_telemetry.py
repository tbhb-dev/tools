"""Launch telemetry must preserve the coordinator's result and output."""

from __future__ import annotations

import json
import socket
import subprocess
import sys
import threading
import time
from typing import TYPE_CHECKING

import pytest
import tbhb_telemetry
from agent_driver_example import execution_arguments, setup
from agent_example import settings

from tbhbdev import telemetry
from tbhbdev.alpha import agent

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from opentelemetry.sdk.trace import ReadableSpan

pytestmark = pytest.mark.usefixtures("isolated_home")  # noqa: V107


def flow(root: Path, monkeypatch: pytest.MonkeyPatch, outcome: str) -> list[str]:
    root.mkdir()
    remote = setup(root, monkeypatch)
    monkeypatch.setattr(agent.settings, "load", settings)
    remote.prompt.write_text("private-prompt-marker")
    if outcome == "refused":
        remote.configure(limits="1000 4 1000 900 100 0")
    elif outcome == "failed":
        remote.configure(launch={"rc": "7", "out.final": "private-ssh-output"})
    elif outcome == "exception":
        remote.prompt.unlink()
    return execution_arguments(remote.prompt)


def broken(*_args: object, **_kwargs: object) -> None:
    message = "telemetry broken"
    raise RuntimeError(message)


@pytest.mark.parametrize("outcome", ["ok", "failed", "refused"])
@pytest.mark.parametrize(
    "fault", ["offline", "stall", "import", "disabled", "endpoint", "attributes"]
)
def test_export_failure_preserves_launch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    outcome: str,
    fault: str,
) -> None:
    monkeypatch.setenv("OTEL_SDK_DISABLED", "true")
    baseline_args = flow(tmp_path / "baseline", monkeypatch, outcome)
    start = time.monotonic()
    baseline_rc = agent.main(baseline_args, "example agent")
    baseline_time = time.monotonic() - start
    baseline_output = capsys.readouterr()

    args = flow(tmp_path / "measured", monkeypatch, outcome)
    monkeypatch.setenv("OTEL_SDK_DISABLED", str(fault == "disabled").lower())
    monkeypatch.delenv("TBHB_TELEMETRY_ALLOW_HOSTS", raising=False)
    with socket.create_server(("localhost", 0)) as listener:
        port = listener.getsockname()[1]
        endpoint = f"http://localhost:{port}"
        release, accepted = threading.Event(), threading.Event()

        def stall() -> None:
            with listener.accept()[0] as connection:
                connection.recv(65536)
                accepted.set()
                release.wait(3)

        thread = threading.Thread(target=stall, daemon=True)
        if fault == "stall":
            listener.settimeout(3)
            thread.start()
        else:
            listener.close()
        if fault == "endpoint":
            endpoint = "http://collector.example:4318"
        elif fault == "import":
            monkeypatch.setitem(sys.modules, "tbhb_telemetry", None)
        elif fault == "attributes":
            monkeypatch.setattr(tbhb_telemetry, "set_attributes", broken)
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", endpoint)
        start = time.monotonic()
        try:
            assert agent.main(args, "example agent") == baseline_rc
            elapsed = time.monotonic() - start
            assert capsys.readouterr() == baseline_output
            # 250 ms export wait plus 750 ms for SDK imports, drop I/O and jitter on a
            # loaded Mac. A stalled export would hold the launch for the full 3 s.
            assert elapsed < baseline_time + 1.0
            if fault == "stall":
                assert accepted.wait(0.5)
        finally:
            release.set()
            if fault == "stall":
                thread.join(3)
                assert not thread.is_alive()
    if fault in {"endpoint", "offline", "stall"}:
        drops = tmp_path / "measured/local-state/tbhb/telemetry/drops.jsonl"
        records = [json.loads(line) for line in drops.read_text().splitlines()]
        # A stalled collector is dropped as a timeout, or as an export error when the
        # SDK gives up on the connection first, which happens on a loaded Mac.
        expected = {
            "endpoint": {"endpoint"},
            "offline": {"export"},
            "stall": {"timeout", "export"},
        }[fault]
        assert records[-1]["reason"] in expected


@pytest.mark.parametrize("outcome", ["ok", "failed", "refused", "exception"])
def test_launch_span_contains_only_allowed_data(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    outcome: str,
) -> None:
    args = flow(tmp_path / "run", monkeypatch, outcome)
    spans: list[ReadableSpan] = []

    def export(batch: list[ReadableSpan], _endpoint: str) -> None:
        spans.extend(batch)

    monkeypatch.setenv("OTEL_SDK_DISABLED", "false")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318")
    monkeypatch.setattr(tbhb_telemetry, "_export", export)
    assert (
        agent.main(args, "example agent")
        == {"ok": 0, "failed": 7, "refused": 1, "exception": 1}[outcome]
    )
    capsys.readouterr()
    assert len(spans) == 1
    span = spans[0]
    assert span.name == "tbhbdev alpha agent exec"
    assert span.end_time is not None
    assert span.start_time is not None
    assert span.end_time > span.start_time
    attributes = dict(span.attributes or {})
    expected = {
        "tbhb.agent.host": "example-linux",
        "tbhb.agent.harness": "codex",
        "tbhb.agent.model": "example-model",
        "tbhb.agent.profile": "build",
        "tbhb.run.id": "sample",
        "tbhb.agent.outcome": "failed" if outcome == "exception" else outcome,
    }
    assert {k: v for k, v in attributes.items() if k in expected} == expected
    assert set(attributes) == set(expected) | {
        "process.executable.name",
        "service.name",
        "service.version",
        "tbhb.cli.command",
        "tbhb.export.dropped",
        "process.exit.code",
    } | ({"error.type"} if outcome != "ok" else set())
    assert not span.events
    assert span.resource.attributes == {
        "service.name": "tbhbdev",
        "service.version": "0",
    }
    assert attributes["process.exit.code"] == (
        0 if outcome == "ok" else 7 if outcome == "failed" else 1
    )


@pytest.mark.parametrize("method", ["resolve", "materialize"])
def test_broken_setup_does_not_change_result(
    monkeypatch: pytest.MonkeyPatch, method: str
) -> None:
    monkeypatch.setenv("OTEL_SDK_DISABLED", "true")
    monkeypatch.setattr(tbhb_telemetry, method, broken)
    assert telemetry.command(lambda: 7) == 7


@pytest.mark.parametrize("after", [False, True])
@pytest.mark.parametrize("exception", [None, ValueError("launch error"), SystemExit(9)])
def test_broken_command_never_retries_or_replaces_launch(
    monkeypatch: pytest.MonkeyPatch, *, after: bool, exception: BaseException | None
) -> None:
    calls: list[str] = []

    def command(_name: str, *, version: str, main: Callable[[], int]) -> None:
        assert version == "0"
        if after:
            main()
        broken()

    def launch() -> int:
        calls.append("launch")
        if exception is not None:
            raise exception
        return 7

    monkeypatch.setattr(tbhb_telemetry, "command", command)
    if exception is None:
        assert telemetry.command(launch) == 7
    else:
        with pytest.raises(type(exception)) as caught:
            telemetry.command(launch)
        assert caught.value is exception
    assert calls == ["launch"]


@pytest.mark.parametrize("args", [["--version"], ["alpha", "agent", "--help"]])
def test_non_exec_does_not_import_telemetry(args: list[str]) -> None:
    script = (
        "import sys; from tbhbdev.cli import main; "
        f"assert main({args!r}) == 0; "
        "assert 'tbhb_telemetry' not in sys.modules; "
        "assert not any(k.startswith('opentelemetry') for k in sys.modules)"
    )
    subprocess.run(
        [sys.executable, "-c", script], check=True, capture_output=True, timeout=10
    )

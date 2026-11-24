"""Launch telemetry must preserve the coordinator's result and output."""

from __future__ import annotations

import io
import json
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import Mock

import pytest
import tbhb_telemetry
from agent_driver_example import execution_arguments, setup
from agent_example import settings

from tbhbdev import telemetry
from tbhbdev.alpha import agent

if TYPE_CHECKING:
    from collections.abc import Callable
    from typing import BinaryIO

    from opentelemetry.sdk.trace import ReadableSpan

ENDPOINT = "OTEL_EXPORTER_OTLP_ENDPOINT"
ALLOW_HOSTS = "TBHB_TELEMETRY_ALLOW_HOSTS"
COLLECTOR = "collector.example.test"
URL = f"http://{COLLECTOR}:4318"
DROP_WARNING = "; warning: the exporter may drop the launch trace"


@pytest.fixture
def disable_telemetry() -> None:
    """Override the suite's telemetry opt-out for these tests."""


@pytest.fixture(autouse=True)
def telemetry_files(
    tmp_path: Path, isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, Path]:
    managed = tmp_path / "managed-settings.json"
    user = isolated_home / "claude-config" / "settings.json"
    user.parent.mkdir()
    monkeypatch.setattr(telemetry, "_MANAGED_SETTINGS", {sys.platform: managed})
    monkeypatch.setattr(telemetry, "_REPORTED", threading.Lock())
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(user.parent))
    monkeypatch.delenv("OTEL_SDK_DISABLED", raising=False)
    for key in (ENDPOINT, ALLOW_HOSTS):
        monkeypatch.setenv(key, "")
        monkeypatch.delenv(key)
    return managed, user


def diagnostic(endpoint: str, hosts: str) -> str:
    warning = DROP_WARNING if "none" in (endpoint, hosts) else ""
    return (
        f"telemetry: {ENDPOINT} from {endpoint}; {ALLOW_HOSTS} from {hosts}{warning}\n"
    )


@pytest.fixture
def local_export(monkeypatch: pytest.MonkeyPatch) -> dict[str, str | None]:
    received: dict[str, str | None] = {}

    def command(_name: str, *, version: str, main: Callable[[], int]) -> int:
        assert version == "0"
        received.update((key, os.environ.get(key)) for key in (ENDPOINT, ALLOW_HOSTS))
        return main()

    monkeypatch.setattr(tbhb_telemetry, "command", command)
    return received


@pytest.mark.parametrize(
    ("process", "managed", "expected"),
    [
        (
            {ENDPOINT: URL, ALLOW_HOSTS: COLLECTOR},
            {ENDPOINT: "managed"},
            ("environment", "environment"),
        ),
        ({}, {ENDPOINT: URL, ALLOW_HOSTS: COLLECTOR}, ("managed", "managed")),
        (
            {ENDPOINT: URL},
            {ENDPOINT: "managed", ALLOW_HOSTS: COLLECTOR},
            ("environment", "managed"),
        ),
        ({ALLOW_HOSTS: ""}, {ENDPOINT: URL}, ("managed", "user")),
        ({}, {}, ("user", "user")),
        ({}, {ENDPOINT: 42, ALLOW_HOSTS: ""}, ("user", "user")),
        ({}, {ENDPOINT: "\0", ALLOW_HOSTS: "\ud800"}, ("user", "user")),
    ],
)
def test_settings_precedence(  # noqa: PLR0913, PLR0917 -- precedence matrix and fixtures
    telemetry_files: tuple[Path, Path],
    local_export: dict[str, str | None],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    process: dict[str, str],
    managed: dict[str, object],
    expected: tuple[str, str],
) -> None:
    managed_file, user_file = telemetry_files
    for key, value in process.items():
        monkeypatch.setenv(key, value)
    user = {ENDPOINT: f"{URL}/user", ALLOW_HOSTS: f"{COLLECTOR},review-host"}
    managed_file.write_text(json.dumps({"env": managed}), encoding="utf-8")
    user_file.write_text(json.dumps({"env": user}), encoding="utf-8")
    opened: list[str] = []
    original_open = os.open

    def track_open(path: str, flags: int) -> int:
        opened.append(path)
        return original_open(path, flags)

    monkeypatch.setattr(os, "open", track_open)

    assert telemetry.command(lambda: 7) == 7

    expected_files = []
    if expected != ("environment", "environment"):
        expected_files.append(str(managed_file))
    if "user" in expected:
        expected_files.append(str(user_file))
    assert opened == expected_files
    values = {"environment": process, "managed": managed, "user": user}
    assert local_export == {
        key: values[source][key]
        for key, source in zip((ENDPOINT, ALLOW_HOSTS), expected, strict=True)
    }
    assert capsys.readouterr() == ("", diagnostic(*expected))


def test_only_allowlisted_keys_apply_from_default_user_settings(
    isolated_home: Path,
    local_export: dict[str, str | None],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv("CLAUDE_CONFIG_DIR")
    monkeypatch.setattr(telemetry, "_MANAGED_SETTINGS", {})
    decoy, marker = "DECOY_SECRET", "fictional-decoy-value"
    monkeypatch.delenv(decoy, raising=False)
    user = isolated_home / ".claude" / "settings.json"
    user.parent.mkdir()
    user.write_text(
        json.dumps(
            {"env": {ENDPOINT: URL, decoy: marker, "OTEL_SDK_DISABLED": "true"}}
        ),
        encoding="utf-8",
    )
    reported = threading.Lock()

    def acquire(*, blocking: bool) -> bool:
        assert blocking is False
        return reported.acquire(blocking=False)

    monkeypatch.setattr(telemetry, "_REPORTED", SimpleNamespace(acquire=acquire))

    assert telemetry.command(lambda: 0) == 0
    assert local_export == {ENDPOINT: URL, ALLOW_HOSTS: None}
    assert decoy not in os.environ
    assert "OTEL_SDK_DISABLED" not in os.environ
    out, err = capsys.readouterr()
    assert out == ""
    assert err == diagnostic("user", "none")
    assert decoy not in err
    assert marker not in err
    assert telemetry.command(lambda: 0) == 0
    assert capsys.readouterr() == ("", "")


@pytest.mark.parametrize(
    "invalid",
    [
        "{",
        "[]",
        '{"env": []}',
        '{"env": {}}',
        pytest.param("[" * 10_000 + "0" + "]" * 10_000, id="deeply-nested"),
    ],
)
@pytest.mark.parametrize("user_available", [False, True])
def test_unreadable_or_invalid_settings_fall_back(  # noqa: PLR0913 -- settings matrix and fixtures
    telemetry_files: tuple[Path, Path],
    local_export: dict[str, str | None],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    invalid: str,
    *,
    user_available: bool,
) -> None:
    managed, user = telemetry_files
    managed.write_text(invalid, encoding="utf-8")
    user.write_text(json.dumps({"env": {ENDPOINT: URL}}), encoding="utf-8")
    original_open = os.open

    def open_file(path: str, flags: int) -> int:
        if path == str(user):
            raise PermissionError
        return original_open(path, flags)

    if not user_available:
        monkeypatch.setattr(os, "open", open_file)
    assert telemetry.command(lambda: 7) == 7
    assert local_export == {
        ENDPOINT: URL if user_available else None,
        ALLOW_HOSTS: None,
    }
    assert capsys.readouterr() == (
        "",
        diagnostic("user" if user_available else "none", "none"),
    )


def test_unavailable_home_and_nonregular_settings_fall_back(
    telemetry_files: tuple[Path, Path],
    local_export: dict[str, str | None],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    managed, _user = telemetry_files
    os.mkfifo(managed)
    original_open = os.open

    def open_fifo(path: str, flags: int) -> int:
        assert flags & os.O_NONBLOCK
        return original_open(path, flags)

    def open_settings(
        path: Path,
        mode: str,
        *,
        buffering: int,
        opener: Callable[[str, int], int] | None = None,
    ) -> BinaryIO:
        assert mode == "rb"
        assert buffering == 0
        assert opener is not None
        return open(path, mode, buffering=buffering, opener=opener)

    monkeypatch.setattr(os, "open", open_fifo)
    monkeypatch.setattr(telemetry, "open", open_settings, raising=False)
    monkeypatch.setattr(json, "loads", pytest.fail)
    monkeypatch.delenv("CLAUDE_CONFIG_DIR")
    monkeypatch.setattr("pathlib.Path.home", broken)
    assert telemetry.command(lambda: 7) == 7
    assert local_export == {ENDPOINT: None, ALLOW_HOSTS: None}
    assert capsys.readouterr() == ("", diagnostic("none", "none"))


@pytest.mark.parametrize(
    ("size", "source"), [(65_536, "managed"), (65_537, "user"), (131_072, "user")]
)
def test_settings_read_is_bounded(
    telemetry_files: tuple[Path, Path],
    local_export: dict[str, str | None],
    capsys: pytest.CaptureFixture[str],
    size: int,
    source: str,
) -> None:
    managed, user = telemetry_files
    settings_json = json.dumps(
        {"env": {ENDPOINT: URL}, "label": "é"}, ensure_ascii=False
    )
    managed.write_bytes(settings_json.encode().ljust(size))
    user.write_text(json.dumps({"env": {ENDPOINT: "fallback"}}), encoding="utf-8")

    assert telemetry.command(lambda: 7) == 7
    assert local_export == {
        ENDPOINT: URL if source == "managed" else "fallback",
        ALLOW_HOSTS: None,
    }
    assert capsys.readouterr() == ("", diagnostic(source, "none"))


@pytest.mark.parametrize("operation", ["write", "flush"])
@pytest.mark.parametrize("error", [OSError, ValueError])
def test_diagnostic_ignores_stream_errors(
    monkeypatch: pytest.MonkeyPatch, operation: str, error: type[Exception]
) -> None:
    stderr = io.StringIO()
    fail = Mock(side_effect=error)
    monkeypatch.setattr(stderr, operation, fail)
    with monkeypatch.context() as patch:
        patch.setattr(sys, "stderr", stderr)
        telemetry._resolve_environment()  # noqa: SLF001 -- bypass the outer safeguard
    assert fail.call_count == 1


def test_diagnostic_writes_to_original_stderr(
    monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
) -> None:
    with monkeypatch.context() as patch:
        patch.setattr(sys, "stderr", sys.__stderr__)
        telemetry._resolve_environment()  # noqa: SLF001 -- exercise the diagnostic
    assert capfd.readouterr() == ("", diagnostic("none", "none"))


@pytest.mark.parametrize("closed", [False, True])
def test_diagnostic_skips_unavailable_stderr(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    local_export: dict[str, str | None],
    *,
    closed: bool,
) -> None:
    stderr = io.StringIO() if closed else None
    if stderr is not None:
        stderr.close()
    with monkeypatch.context() as patch:
        patch.setattr(sys, "stderr", stderr)
        assert telemetry.command(lambda: 7) == 7
    assert capsys.readouterr() == ("", "")
    assert telemetry.command(lambda: 7) == 7
    assert capsys.readouterr() == ("", diagnostic("none", "none"))
    assert local_export == {ENDPOINT: None, ALLOW_HOSTS: None}


@pytest.mark.parametrize("target", ["pipe", "full", "closed"])
def test_diagnostic_preserves_process_exit_and_stdout(target: str) -> None:
    device = Path("/dev/full")
    if target == "full" and not device.is_char_device():
        pytest.skip("/dev/full is unavailable")
    script = (
        "import os, sys; from tbhbdev import telemetry; "
        "telemetry._MANAGED_SETTINGS.clear(); "
        "sys.modules['tbhb_telemetry'] = None; "
        + ("os.close(2); " if target == "closed" else "")
        + "raise SystemExit(telemetry.command(lambda: print('launch') or 7))"
    )
    with (device if target == "full" else Path(os.devnull)).open("wb") as sink:
        result = subprocess.run(
            [sys.executable, "-c", script],
            stdout=subprocess.PIPE,
            stderr=sink if target == "full" else subprocess.PIPE,
            check=False,
            timeout=10,
        )
    assert result.returncode == 7
    assert result.stdout == b"launch\n"
    if target != "full":
        assert result.stderr == (
            diagnostic("none", "none").encode() if target == "pipe" else b""
        )


@pytest.mark.parametrize("disabled", ["true", "TRUE"])
def test_disabled_skips_lookup_and_reporting(
    local_export: dict[str, str | None],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    disabled: str,
) -> None:
    monkeypatch.setenv("OTEL_SDK_DISABLED", disabled)
    monkeypatch.setattr(telemetry, "_apply_settings", pytest.fail)
    assert telemetry.command(lambda: 0) == 0
    assert local_export == {ENDPOINT: None, ALLOW_HOSTS: None}
    assert capsys.readouterr() == ("", "")


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


@pytest.mark.parametrize(
    ("otel_endpoint", "tbhb_endpoint", "expected_endpoint"),
    [
        (None, " https://collector.example\t", " https://collector.example\t"),
        ("https://collector.example/otel", None, "https://collector.example/otel"),
        (
            " https://collector.example/otel ",
            "https://collector.example",
            " https://collector.example/otel ",
        ),
        ("", "https://collector.example", "https://collector.example"),
        (" \t", "https://collector.example", "https://collector.example"),
        (None, None, None),
        (None, "", None),
        (None, " \t", None),
        ("", "", ""),
        (" \t", " \t", " \t"),
    ],
)
def test_endpoint_default(
    monkeypatch: pytest.MonkeyPatch,
    otel_endpoint: str | None,
    tbhb_endpoint: str | None,
    expected_endpoint: str | None,
) -> None:
    otel_name = "OTEL_EXPORTER_OTLP_ENDPOINT"
    tbhb_name = "TBHB_TELEMETRY_ENDPOINT"
    monkeypatch.setenv(otel_name, otel_endpoint or "")
    if otel_endpoint is None:
        monkeypatch.delenv(otel_name)
    if tbhb_endpoint is None:
        monkeypatch.delenv(tbhb_name, raising=False)
    else:
        monkeypatch.setenv(tbhb_name, tbhb_endpoint)
    expected_environment = dict(os.environ)
    if expected_endpoint is not None:
        expected_environment[otel_name] = expected_endpoint
    captured_environment: dict[str, str] = {}

    def command(_name: str, *, main: Callable[[], int], **_kwargs: str) -> int:
        captured_environment.update(os.environ)
        return main()

    monkeypatch.setattr(tbhb_telemetry, "command", command)
    assert telemetry.command(lambda: 7) == 7
    assert captured_environment == expected_environment
    assert dict(os.environ) == expected_environment


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
            endpoint = URL
        elif fault == "import":
            monkeypatch.setitem(sys.modules, "tbhb_telemetry", None)
        elif fault == "attributes":
            monkeypatch.setattr(tbhb_telemetry, "set_attributes", broken)
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", endpoint)
        start = time.monotonic()
        try:
            assert agent.main(args, "example agent") == baseline_rc
            elapsed = time.monotonic() - start
            assert capsys.readouterr() == (
                baseline_output.out,
                ("" if fault == "disabled" else diagnostic("environment", "none"))
                + baseline_output.err,
            )
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


@pytest.mark.parametrize("outcome", [0, 7])
def test_resolver_failure_preserves_launch(
    tmp_path: Path,
    local_export: dict[str, str | None],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    outcome: int,
) -> None:
    def launch(*_args: object) -> int:
        print("launch")  # noqa: T201 -- exercise the CLI's standard output
        return outcome

    monkeypatch.setattr(telemetry, "_resolve_environment", broken)
    monkeypatch.setattr(agent, "dispatch", launch)
    assert (
        agent.main(execution_arguments(tmp_path / "prompt"), "example agent") == outcome
    )
    assert capsys.readouterr() == ("launch\n", "")
    assert local_export == {ENDPOINT: None, ALLOW_HOSTS: None}


@pytest.mark.parametrize("outcome", ["ok", "failed", "refused", "exception"])
def test_launch_span_contains_only_allowed_data(
    tmp_path: Path,
    telemetry_files: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    outcome: str,
) -> None:
    args = flow(tmp_path / "run", monkeypatch, outcome)
    spans: list[ReadableSpan] = []

    def export(batch: list[ReadableSpan], endpoint: str) -> None:
        assert endpoint == URL
        spans.extend(batch)

    monkeypatch.setenv("OTEL_SDK_DISABLED", "false")
    _managed, user = telemetry_files
    user.write_text(
        json.dumps({"env": {ENDPOINT: URL, ALLOW_HOSTS: COLLECTOR}}), encoding="utf-8"
    )
    monkeypatch.setattr(tbhb_telemetry, "_export", export)
    assert (
        agent.main(args, "example agent")
        == {"ok": 0, "failed": 7, "refused": 1, "exception": 1}[outcome]
    )
    assert capsys.readouterr().err.startswith(diagnostic("user", "user"))
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
    results: list[int] = []

    def command(_name: str, *, main: Callable[[], int], **_kwargs: str) -> None:
        results.append(main())

    monkeypatch.delenv("TBHB_TELEMETRY_ENDPOINT", raising=False)
    monkeypatch.setattr(tbhb_telemetry, "command", command)
    monkeypatch.setattr(tbhb_telemetry, method, broken)
    assert telemetry.command(lambda: 7) == 7
    assert results == [7]


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

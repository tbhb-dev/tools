"""Captured-style streams, redaction, OTLP shape and command contracts."""

import json
import sys
from pathlib import Path
from typing import cast

import httpx
import pytest

from tbhbdev import agy, cli

FIXTURES = Path(__file__).parent / "data" / "agy"
MARKER = "marker-secret-must-stay-local"


@pytest.fixture
def fake(tmp_path: Path) -> Path:
    script = tmp_path / "agy"
    script.write_text(
        f"#!{sys.executable}\nfrom pathlib import Path\nimport sys\n"
        "p=Path(__file__).parent\n"
        "name='version' if '--version' in sys.argv else 'stream'\n"
        "print((p/name).read_text())\n"
        "sys.exit(int((p/'rc').read_text()))\n"
    )
    script.chmod(0o700)
    (tmp_path / "version").write_text("1.5.8")
    (tmp_path / "rc").write_text("0")
    return script


def attributes(items: list[dict[str, object]]) -> dict[str, object]:
    return {
        str(a["key"]): next(iter(cast("agy.Json", a["value"]).values())) for a in items
    }


@pytest.mark.parametrize("fixture", ["plain", "file"])
def test_record(fake: Path, tmp_path: Path, fixture: str) -> None:
    (tmp_path / "stream").write_text((FIXTURES / f"{fixture}.jsonl").read_text())
    out = tmp_path / "record"
    command = [str(fake), "--model", "gemini-4-argon-high"]
    assert cli.main(["agy", "record", "--dir", str(out), "--", *command]) == 0
    traces = json.loads((out / "otel/traces.json").read_text())["resourceSpans"][0]
    metrics = json.loads((out / "otel/metrics.json").read_text())["resourceMetrics"][0]
    assert traces["resource"] == metrics["resource"]
    expected = json.loads((FIXTURES / "resource.json").read_text())
    expected["host.name"] = agy.socket.gethostname()
    expected["tbhb.account"] = agy.getpass.getuser()
    assert attributes(traces["resource"]["attributes"]) == expected
    spans = traces["scopeSpans"][0]["spans"]
    root = spans[0]
    assert (root["name"], root["kind"]) == ("invoke_agent antigravity", 3)
    assert len(spans) == (3 if fixture == "plain" else 4)
    assert {s["parentSpanId"] for s in spans[1:]} == {root["spanId"]}
    assert ("status" in root) == (fixture == "file")
    assert (out / "agy.stdout").read_text() == ("ready" if fixture == "plain" else "")
    assert (out / "agy.rc").read_text() == "0\n"
    histograms = metrics["scopeMetrics"][0]["metrics"]
    assert [m["histogram"]["dataPoints"][0]["sum"] for m in histograms[:2]] == (
        [8654, 25] if fixture == "plain" else [8663, 234]
    )
    for metric in histograms:
        assert metric["histogram"]["aggregationTemporality"] == 1
        point = metric["histogram"]["dataPoints"][0]
        assert set(attributes(point["attributes"])) <= {
            "gen_ai.operation.name",
            "gen_ai.provider.name",
            "gen_ai.request.model",
            "gen_ai.token.type",
        }


def test_secrets_unknown_fields_and_states(
    fake: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    event: agy.Json = {
        "step_index": 1,
        "state": "DONE",
        **dict.fromkeys(
            ("step_type", "tool_name", "text_delta", "subagent_info", "unknown"), MARKER
        ),
        "tool_info": {
            "name": MARKER,
            "parameters": {"x": MARKER},
            "output": MARKER,
            "error": {"message": MARKER},
        },
        "duration_seconds": MARKER,
        "usage": {"input_tokens": MARKER, "secret": 123},
    }
    cleaned = agy.clean(event, "step_update")
    assert cleaned["tool_name"] == "other"
    assert cleaned["parameters_bytes"] == len(json.dumps({"x": MARKER}).encode())
    assert cleaned["output_bytes"] == len(MARKER)
    assert "unknown" not in cleaned
    assert cleaned["usage"] == {}
    assert agy.closed(7, agy.STEPS) == "other"
    result = {"status": "SUCCESS", "finished_reason": MARKER, "response": MARKER}
    stream: list[agy.Json] = [{"event": "init", "conversation_id": MARKER}]
    stream.extend(
        [
            {"event": "step_update", "step_update": d}
            for d in (event, {**event, "state": MARKER})
        ]
    )
    stream.append({"event": "result", "result": result})
    (tmp_path / "stream").write_text("bad-json\n" + "\n".join(map(json.dumps, stream)))
    (tmp_path / "version").write_text(MARKER)
    monkeypatch.chdir(tmp_path)
    assert agy.main(["record", str(fake)], "agy") == 0
    combined = "".join(p.read_text() for p in (tmp_path / "otel").glob("*.json"))
    assert MARKER not in combined
    assert '"service.version", "value": {"stringValue": "other"}' in combined
    assert '"tbhb.agent.record.dropped_states", "value": {"intValue": "1"}' in combined
    assert (tmp_path / "agy.stdout").read_text() == MARKER
    (tmp_path / "stream").write_text("")
    (tmp_path / "rc").write_text("7")
    assert agy.record(tmp_path, [str(fake), "--model", "gemini-pro-orchestrator"]) == 7


@pytest.mark.parametrize(
    ("result", "rc", "response", "expected"),
    [
        ({"finished_reason": "print_timeout"}, 0, "", "print_timeout"),
        ({}, 3, "", "agent_error"),
        ({"finished_reason": "error"}, 0, "", "agent_error"),
        ({"status": "other"}, 0, "", "agent_error"),
    ],
)
def test_errors(result: agy.Json, rc: int, response: str, expected: str) -> None:
    assert agy.error_type(result, rc, response) == expected


def test_export(
    fake: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    (tmp_path / "stream").write_text((FIXTURES / "plain.jsonl").read_text())
    agy.record(tmp_path, [str(fake)])
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    assert agy.main(["export", str(tmp_path)], "agy") == 2
    assert "is unset" in capsys.readouterr().err
    sent: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(200 if len(sent) < 3 else 503)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(agy.httpx, "post", client.post)
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector:4318/")
    assert agy.export(tmp_path) == 0
    assert agy.export(tmp_path) == 1
    assert [str(r.url) for r in sent[:2]] == [
        "http://collector:4318/v1/traces",
        "http://collector:4318/v1/metrics",
    ]
    assert all(r.headers["Content-Type"] == "application/json" for r in sent)
    assert sent[0].content == (tmp_path / "otel/traces.json").read_bytes()
    assert (
        capsys.readouterr().out
        == "traces: HTTP 200\nmetrics: HTTP 200\ntraces: HTTP 503\nmetrics: HTTP 503\n"
    )
    with pytest.raises(SystemExit, match="2"):
        agy.main(["record"], "agy")

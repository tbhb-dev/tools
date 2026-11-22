"""Record plain-answer and file-tool agy runs as OTLP/JSON."""

import argparse
import getpass
import json
import os
import re
import secrets
import socket
import subprocess  # nosec B404
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING, cast

import httpx

from tbhbdev.alpha.identity import AGY_MODELS

if TYPE_CHECKING:
    from typing import TextIO

type Json = dict[str, object]
TOKENS = tuple(
    f"{k}_tokens" for k in ("input", "output", "thinking", "cache_read", "total")
)
STEPS = {"user_input", "agent_response", "tool"}
TOOLS = {"write_file"}
AGENT_ERROR = 3
REASONS = {"print_timeout", "interrupted", "error"}


def closed(value: object, inventory: set[str]) -> str:
    return value if isinstance(value, str) and value in inventory else "other"


def numbers(data: Json, keys: tuple[str, ...]) -> Json:
    return {k: v for k, v in data.items() if k in keys and type(v) in {int, float}}


def clean(data: Json, kind: str) -> Json:
    """Discard free text and unknown fields before making telemetry records."""
    index = {"step_update": "step_index", "result": "num_turns"}[kind]
    kept = numbers(data, (index, "duration_seconds"))
    fields = {
        "step_update": {
            "state": {"ACTIVE", "DONE"},
            "step_type": STEPS,
            "tool_name": TOOLS,
        },
        "result": {"finished_reason": REASONS, "status": {"SUCCESS"}},
    }
    for key, inventory in fields[kind].items():
        if key in data:
            kept[key] = closed(data[key], inventory)
    usage = cast("Json", data.get("usage", {}))
    kept["usage"] = numbers(usage, TOKENS)
    tool = cast("Json", data.get("tool_info", {}))
    if kind == "step_update" and tool:
        kept["tool_name"] = closed(tool.get("name"), TOOLS)
        for key in ("parameters", "output"):
            value = tool.get(key, "")
            encoded = (
                value
                if isinstance(value, str)
                else json.dumps(value, ensure_ascii=False)
            )
            kept[f"{key}_bytes"] = len(encoded.encode())
    return kept


def attrs(values: Json) -> list[Json]:
    return [
        {
            "key": k,
            "value": {"stringValue" if isinstance(v, str) else "intValue": str(v)},
        }
        for k, v in values.items()
    ]


def span(  # noqa: PLR0913, PLR0917 -- OTLP span fields.
    name: str, start: int, end: int, values: Json, trace: str, parent: str = ""
) -> Json:
    return {
        "name": name,
        "kind": 1 if parent else 3,
        "traceId": trace,
        "spanId": secrets.token_hex(8),
        "parentSpanId": parent,
        "startTimeUnixNano": str(start),
        "endTimeUnixNano": str(end),
        "attributes": attrs(values),
    }


def error_type(result: Json, rc: int, response: str) -> str:
    reason = result.get("finished_reason")
    if reason == "print_timeout":
        return "print_timeout"
    failed = result.get("status", "SUCCESS") != "SUCCESS"
    if rc == AGENT_ERROR or reason == "error" or failed:
        return "agent_error"
    if rc:
        return f"exit_{rc}"
    return "no_output" if not response else ""


def metrics(labels: Json, usage: Json, start: int, end: int) -> list[Json]:
    items: list[Json] = []
    for category in ("input", "output", "duration"):
        duration = category == "duration"
        suffix = "operation.duration" if duration else "token.usage"
        name = f"gen_ai.client.{suffix}"
        extra = {} if duration else {"gen_ai.token.type": category}
        value = (end - start) / 1e9 if duration else usage.get(f"{category}_tokens", 0)
        point = {
            "attributes": attrs({**labels, **extra}),
            "startTimeUnixNano": str(start),
            "timeUnixNano": str(end),
            "count": "1",
            "sum": value,
            "bucketCounts": ["1"],
        }
        metric: Json = {"name": name, "unit": "s" if duration else "{token}"}
        metric["histogram"] = {"aggregationTemporality": 1, "dataPoints": [point]}
        items.append(metric)
    return items


def telemetry(  # noqa: PLR0913, PLR0917 -- One run and its measured results.
    command: list[str],
    result: Json,
    steps: dict[int, tuple[int, int, Json]],
    start: int,
    end: int,
    rc: int,
    response: str,
    dropped: int,
) -> tuple[Json, Json]:
    model_key = (
        command[command.index("--model") + 1] if "--model" in command else "other"
    )
    provider, model, effort = AGY_MODELS.get(model_key, ("other", "other", "other"))
    labels: Json = {
        "gen_ai.operation.name": "invoke_agent",
        "gen_ai.provider.name": provider,
        "gen_ai.request.model": model,
    }
    usage = cast("Json", result.get("usage", {}))
    values: Json = {
        **labels,
        "process.exit.code": rc,
        "tbhb.agent.effort": effort or "other",
        "tbhb.agent.record.dropped_states": dropped,
    }
    for key, value in usage.items():
        prefix = "gen_ai" if key in {"input_tokens", "output_tokens"} else "tbhb.agent"
        values[f"{prefix}.usage.{key}"] = value
    for key, target in (
        ("finished_reason", "tbhb.agent.finished_reason"),
        ("num_turns", "tbhb.agent.turns"),
    ):
        if key in result:
            values[target] = result[key]
    trace = secrets.token_hex(16)
    root = span("invoke_agent antigravity", start, end, values, trace)
    error = error_type(result, rc, response)
    if error:
        values["error.type"] = error
        root.update(attributes=attrs(values), status={"code": 2})
    spans = [root]
    for index, (first, last, step) in steps.items():
        name = str(step.get("tool_name", step.get("step_type", "other")))
        details: Json = {"tbhb.agent.step.index": index}
        details.update({k: v for k, v in step.items() if k.endswith("_bytes")})
        spans.append(span(name, first, last, details, trace, str(root["spanId"])))
    version = subprocess.run(  # nosec B603
        [command[0], "--version"], capture_output=True, text=True, check=False
    ).stdout.strip()
    resource_values: Json = {
        "service.name": "antigravity",
        "service.namespace": "agents",
        "host.name": socket.gethostname(),
        "tbhb.account": getpass.getuser(),
        "service.version": version
        if re.fullmatch(r"[0-9]{1,3}(\.[0-9]{1,4}){2}", version)
        else "other",
        "deployment.environment.name": "lab",
        "telemetry.sdk.name": "tbhbdev-agy-record",
    }
    resource = {"attributes": attrs(resource_values)}
    traces = {"resource": resource, "scopeSpans": [{"spans": spans}]}
    measurements = {
        "resource": resource,
        "scopeMetrics": [{"metrics": metrics(labels, usage, start, end)}],
    }
    return {"resourceSpans": [traces]}, {"resourceMetrics": [measurements]}


def record(directory: Path, command: list[str]) -> int:
    directory.joinpath("otel").mkdir(parents=True, exist_ok=True)
    start = time.time_ns()
    steps: dict[int, tuple[int, int, Json]] = {}
    result: Json = {}
    response = ""
    dropped = 0
    with subprocess.Popen(
        [*command, "--output-format", "stream-json"], stdout=subprocess.PIPE, text=True
    ) as process:  # nosec B603
        stdout: TextIO = cast("TextIO", process.stdout)
        for line in stdout:
            stamp = time.time_ns()
            try:
                event = json.loads(line)
            except ValueError:
                continue
            kind = event.get("event")
            if kind == "result":
                response = event["result"].get("response", "")
                result = clean(event["result"], "result")
            elif kind == "step_update":
                step = clean(event["step_update"], "step_update")
                if step.get("state") == "other":
                    dropped += 1
                    continue
                index = cast("int", step["step_index"])
                elapsed = int(cast("float", step.get("duration_seconds", 0)) * 1e9)
                first = steps.get(index, (max(start, stamp - elapsed), stamp, step))[0]
                steps[index] = (first, stamp, step)
        rc = process.wait()
    end = time.time_ns()
    directory.joinpath("agy.stdout").write_text(response)
    directory.joinpath("agy.rc").write_text(f"{rc}\n")
    traces, metrics = telemetry(
        command, result, steps, start, end, rc, response, dropped
    )
    for signal, payload in (("traces", traces), ("metrics", metrics)):
        directory.joinpath("otel", f"{signal}.json").write_text(json.dumps(payload))
    return rc


def export(directory: Path) -> int:
    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
    if not endpoint:
        print("OTEL_EXPORTER_OTLP_ENDPOINT is unset", file=sys.stderr)
        return 2
    failed = False
    for signal in ("traces", "metrics"):
        response = httpx.post(
            f"{endpoint.rstrip('/')}/v1/{signal}",
            content=directory.joinpath("otel", f"{signal}.json").read_bytes(),
            headers={"Content-Type": "application/json"},
            timeout=30,
        )
        print(f"{signal}: HTTP {response.status_code}")
        failed |= not response.is_success
    return int(failed)


def main(argv: list[str], prog: str) -> int:
    parser = argparse.ArgumentParser(prog=prog)
    sub = parser.add_subparsers(dest="action", required=True)
    rec = sub.add_parser("record")
    rec.add_argument("--dir", type=Path, default=Path.cwd())
    rec.add_argument("command", nargs=argparse.REMAINDER)
    sub.add_parser("export").add_argument("directory", type=Path)
    args = parser.parse_args(argv)
    if args.action == "export":
        return export(args.directory)
    command = args.command
    if command[:1] == ["--"]:
        command = command[1:]
    if not command:
        parser.error("record requires a command after --")
    return record(args.dir, command)

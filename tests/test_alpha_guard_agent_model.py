# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns
"""Tests for the Agent tool model guard."""

import io
import json
import sys
from pathlib import Path

import pytest

from tbhbdev import alpha
from tbhbdev.alpha import guard_agent_model as guard


def run(
    stdin: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> tuple[int, str, str]:
    monkeypatch.setattr(sys, "stdin", io.StringIO(stdin))
    code = alpha.main(["guard-agent-model"], "tbhbdev alpha")
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def payload(tool_name: str, tool_input: object) -> str:
    return json.dumps({"tool_name": tool_name, "tool_input": tool_input})


@pytest.mark.parametrize(
    "tool_input",
    [
        {},
        {"prompt": "x"},
        {"model": ""},
        {"model": "  "},
        {"model": None},
        {"model": 5},
        {"subagent_type": "general-purpose"},
        "not an object",
        None,
    ],
)
def test_missing_model_is_refused(
    tool_input: object,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    code, out, err = run(payload("Agent", tool_input), monkeypatch, capsys)
    assert (code, out) == (2, "")
    assert err == guard.REASON_MODEL + "\n"
    assert "sonnet" in err
    assert "opus or fable only for planning and design" in err


def test_missing_tool_input_is_refused(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    code, _, err = run('{"tool_name": "Agent"}', monkeypatch, capsys)
    assert code == 2
    assert err == guard.REASON_MODEL + "\n"


def entry(model: str | None, role: str = "assistant") -> str:
    message: dict[str, object] = {"role": role}
    if model is not None:
        message["model"] = model
    return json.dumps({"type": role, "message": message})


def fork_payload(path: object, **extra: object) -> str:
    data: dict[str, object] = {
        "tool_name": "Agent",
        "tool_input": {"subagent_type": "fork", **extra},
    }
    if path is not None:
        data["transcript_path"] = str(path) if isinstance(path, Path) else path
    return json.dumps(data)


def transcript(tmp_path: Path, *lines: str) -> Path:
    path = tmp_path / "session.jsonl"
    path.write_text("\n".join(lines) + "\n")
    return path


@pytest.mark.parametrize("model", ["claude-sonnet-5-5", "claude-haiku-5-5"])
def test_fork_on_a_cheap_session_passes(
    model: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = transcript(tmp_path, entry("claude-opus-5-5"), entry(model))
    assert run(fork_payload(path), monkeypatch, capsys) == (0, "", "")


@pytest.mark.parametrize("model", ["claude-opus-5-5", "claude-fable-5-2", "Opus"])
def test_fork_on_a_costly_session_is_refused(
    model: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = transcript(tmp_path, entry("claude-sonnet-5-5"), entry(model))
    code, out, err = run(fork_payload(path, model="sonnet"), monkeypatch, capsys)
    assert (code, out) == (2, "")
    assert err == guard.reason_fork(model) + "\n"
    assert model in err


def test_synthetic_last_line_is_skipped(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = transcript(tmp_path, entry("claude-opus-5-5"), entry("<synthetic>"))
    code, _, err = run(fork_payload(path), monkeypatch, capsys)
    assert code == 2
    assert "claude-opus-5-5" in err


def test_malformed_lines_are_skipped(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = transcript(
        tmp_path,
        entry("claude-sonnet-5-5"),
        "not json",
        "[]",
        json.dumps({"message": "text"}),
        entry(None, "user"),
        entry(""),
    )
    assert run(fork_payload(path), monkeypatch, capsys) == (0, "", "")


def test_only_the_file_tail_is_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    filler = entry(None, "user") + " " * 1000
    lines = [entry("claude-opus-5-5")] + [filler] * (guard.TAIL // 1000 + 10)
    path = transcript(tmp_path, *lines)
    code, _, err = run(fork_payload(path), monkeypatch, capsys)
    assert (code, err) == (2, guard.reason_fork(None) + "\n")


@pytest.mark.parametrize(
    "kind", ["no-path", "empty", "not-a-string", "missing", "dir", "nul"]
)
def test_fork_with_an_unreadable_transcript_is_refused(
    kind: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path: object = {
        "no-path": None,
        "empty": "",
        "not-a-string": 5,
        "missing": tmp_path / "gone.jsonl",
        "dir": tmp_path,
        "nul": "a\0b",
    }[kind]
    code, out, err = run(fork_payload(path), monkeypatch, capsys)
    assert (code, out) == (2, "")
    assert err == guard.reason_fork(None) + "\n"
    assert "unknown" in err


def test_fork_with_no_model_line_is_refused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = transcript(tmp_path, entry(None, "user"), entry("<synthetic>"))
    code, _, err = run(fork_payload(path), monkeypatch, capsys)
    assert (code, err) == (2, guard.reason_fork(None) + "\n")


@pytest.mark.parametrize("model", ["sonnet", "opus", "fable", "claude-sonnet-5-5"])
@pytest.mark.parametrize("subagent_type", [None, "Explore", "general-purpose"])
def test_launch_with_model_passes(
    model: str,
    subagent_type: str | None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    tool_input = {"model": model}
    if subagent_type:
        tool_input["subagent_type"] = subagent_type
    assert run(payload("Agent", tool_input), monkeypatch, capsys) == (0, "", "")


@pytest.mark.parametrize(
    "stdin",
    [
        payload("Bash", {"command": "ls"}),
        payload("Task", {}),
        payload("Agent2", {"subagent_type": "fork"}),
        "{}",
        "[]",
        "",
        "not json",
    ],
)
def test_other_tools_and_unreadable_input_pass(
    stdin: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert run(stdin, monkeypatch, capsys) == (0, "", "")

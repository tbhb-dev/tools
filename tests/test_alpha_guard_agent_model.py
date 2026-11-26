# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns
"""Tests for the Agent tool model guard."""

import io
import json
import sys

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


@pytest.mark.parametrize("model", [None, "", "sonnet", "opus"])
def test_fork_is_refused(
    model: str | None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    tool_input = {"subagent_type": "fork"}
    if model is not None:
        tool_input["model"] = model
    code, out, err = run(payload("Agent", tool_input), monkeypatch, capsys)
    assert (code, out) == (2, "")
    assert err == guard.REASON_FORK + "\n"


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

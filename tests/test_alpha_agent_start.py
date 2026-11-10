"""Interactive launches use the driver's transfer and launch confirmation."""

from __future__ import annotations

import dataclasses
import json
from typing import TYPE_CHECKING

import pytest
from agent_driver_example import execution_arguments, setup
from agent_example import settings

from tbhbdev.alpha import agent
from tbhbdev.alpha.agent import driver, render, transport
from tbhbdev.alpha.agent.model import RunKind

if TYPE_CHECKING:
    from pathlib import Path

    from agent_driver_example import Remote

CONFIG = settings()


@pytest.fixture
def remote(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Remote:
    result = setup(tmp_path, monkeypatch)
    monkeypatch.setattr(agent.settings, "load", lambda: CONFIG)
    return result


def arguments(remote: Remote, *options: str) -> list[str]:
    return ["start", *execution_arguments(remote.prompt, *options)[1:]]


def test_start_refuses_schema(
    remote: Remote, capsys: pytest.CaptureFixture[str]
) -> None:
    assert agent.main(arguments(remote, "--schema", "schema.json"), "agent") == 2
    assert "unrecognized arguments" in capsys.readouterr().err
    assert not remote.records("ssh")


def test_start_transfers_and_returns_after_launch(
    remote: Remote, capsys: pytest.CaptureFixture[str]
) -> None:
    assert agent.main(arguments(remote), "agent") == 0
    assert capsys.readouterr() == (
        (
            "sample: launched host=example-linux account=agent\n"
            "sample: attach with tbhbdev alpha agent attach sample\n"
        ),
        "",
    )
    run = driver.load(CONFIG, "sample")
    assert run.request.kind is RunKind.START
    record = json.loads((remote.state / "sample.request.json").read_text())
    assert record["kind"] == "start"
    assert (remote.state / "sample.prompt").read_text() == remote.prompt.read_text()
    assert not (remote.state / "sample.schema.json").exists()
    assert not (remote.state / "sample.rc").exists()
    assert len(remote.records("ssh")) == 4
    assert json.loads((remote.root / "control.json").read_text())["session"] is True


def test_start_unsandboxed(remote: Remote, capsys: pytest.CaptureFixture[str]) -> None:
    assert agent.main(arguments(remote, "--unsandboxed", "o/r#1"), "agent") == 0
    assert capsys.readouterr() == (
        (
            "sample: launched host=example-linux account=agent\n"
            "sample: sandbox=none authorized_by=o/r#1\n"
            "sample: attach with tbhbdev alpha agent attach sample\n"
        ),
        "",
    )


@pytest.mark.parametrize("code", [0, 7])
def test_start_early_exit(
    remote: Remote, capsys: pytest.CaptureFixture[str], code: int
) -> None:
    remote.configure(launch={"rc": str(code), "log": "failed\n"})
    assert agent.main(arguments(remote), "agent") == code
    assert capsys.readouterr() == (f"sample: rc={code}\nfailed\n", "")


def test_start_checks_before_contact(remote: Remote) -> None:
    assert agent.main(arguments(remote, "--ref", "main"), "agent") == 1
    assert not remote.records("ssh")


def test_start_parser_defaults(remote: Remote) -> None:
    parsed = agent.parser("agent").parse_args(arguments(remote))
    assert vars(parsed) == {
        "verb": "start",
        "host": "example-linux",
        "repo": "example-org/example-repo",
        "ref": None,
        "harness": "codex",
        "allow_non_gemini": False,
        "account": None,
        "model": "example-model",
        "effort": "high",
        "profile": "build",
        "reason": None,
        "claude_profile": None,
        "unsandboxed": None,
        "timeout": None,
        "schema": None,
        "class_": None,
        "variant": None,
        "role": None,
        "name": "sample",
        "prompt_file": str(remote.prompt),
    }


def test_unsandboxed_formatter_filters_controls() -> None:
    assert render.unsandboxed("na\x1bme", "o/r#1\x07") == (
        "name: sandbox=none authorized_by=o/r#1"
    )


def test_start_launch_filters_controls(
    remote: Remote, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    args = agent.parser("agent").parse_args(arguments(remote))
    run = driver.prepare(CONFIG, args)
    run.name = "sa\x1bmple"
    run.host = dataclasses.replace(run.host, name="host\x07", account="agent\x7f")
    monkeypatch.setattr(
        transport,
        "read",
        lambda *_args, **_kwargs: transport.Reading(
            "running", None, launched=True, session=True, text=""
        ),
    )
    assert driver.poll_launch(CONFIG, run, float("inf")) == 0
    assert capsys.readouterr() == (
        (
            "sample: launched host=host account=agent\n"
            "sample: attach with tbhbdev alpha agent attach sample\n"
        ),
        "",
    )

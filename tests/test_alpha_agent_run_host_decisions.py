"""Run-host decisions exercised with literal inputs."""

from __future__ import annotations

import json
import tomllib

import pytest
from agent_host_example import run_request

from tbhbdev.alpha.agent.decisions import run_host as decisions
from tbhbdev.alpha.agent.model import (
    AgyPolicy,
    Harness,
    Invalid,
    Policy,
    Profile,
    RunKind,
)


@pytest.fixture(autouse=True)
def no_operator_credentials() -> None:
    """These tests need no on-disk credential stub."""


@pytest.fixture(autouse=True)
def config_file() -> None:
    """These tests need no temporary configuration directory."""


@pytest.mark.parametrize(
    ("harness", "opt_out", "unsandboxed", "expected"),
    [
        (Harness.CODEX, None, False, (None, True)),
        (Harness.CODEX, None, True, (None, False)),
        (
            Harness.CODEX,
            "exception",
            False,
            ("codex sandbox opt-out: exception", False),
        ),
        (Harness.CODEX, "exception", True, ("codex sandbox opt-out: exception", False)),
        (Harness.CLAUDE, "exception", False, (None, False)),
        (Harness.AGY, None, False, (None, False)),
    ],
)
def test_proxy_preflight(
    harness: Harness,
    opt_out: str | None,
    unsandboxed: bool,
    expected: tuple[str | None, bool],
) -> None:
    assert (
        decisions.proxy_preflight(harness, opt_out, unsandboxed=unsandboxed) == expected
    )


def test_check_proxy_support() -> None:
    decisions.check_proxy_support(supported=True)
    with pytest.raises(Invalid) as caught:
        decisions.check_proxy_support(supported=False)
    assert str(caught.value) == (
        "Codex network_proxy support required (minimum 0.164.0); "
        "upgrade or configure codex_network_proxy_opt_out"
    )


@pytest.mark.parametrize("harness", Harness)
@pytest.mark.parametrize("unsandboxed", [False, True])
def test_hook_environment(harness: Harness, unsandboxed: bool) -> None:
    env = {"PATH": "/launch/bin", "GIT_DIR": "/example/repo/.git", "OTHER": "value"}
    value = Policy(harness, Profile.BUILD, env, (), AgyPolicy(()), "/tools/bin")
    expected = env
    if harness is Harness.CODEX:
        expected = {"PATH": "/tools/bin", "GIT_CONFIG_COUNT": "0", "OTHER": "value"}
    elif harness is Harness.CLAUDE and not unsandboxed:
        expected = env | {"PATH": "/tools/bin"}
    assert decisions.hook_environment(value, unsandboxed=unsandboxed) == expected


@pytest.mark.parametrize("scheme", ["http", "https"])
@pytest.mark.parametrize("suffix", ["", "/"])
def test_otel_config_derives_signal_paths_from_the_base(
    scheme: str, suffix: str
) -> None:
    base = f"{scheme}://otlp.example.net"
    table = tomllib.loads(decisions.otel_config(base + suffix))["otel"]
    assert table["trace_exporter"]["otlp-http"]["endpoint"] == (f"{base}/v1/traces")
    assert table["metrics_exporter"]["otlp-http"]["endpoint"] == (f"{base}/v1/metrics")


@pytest.mark.parametrize("prepared", [False, True])
@pytest.mark.parametrize("rc", [None, 0, 7])
def test_index_metadata_records_complete_outcome(
    prepared: bool, rc: int | None
) -> None:
    value = run_request(
        harness="claude",
        claude_profile="primary",
        reason="example reason",
        **{"class": "example-class", "variant": "example-variant", "role": "worker"},
    )
    workspace = decisions.IndexWorkspace(
        "/example/worktree", "secondary", gh_fallback=True
    )
    assert decisions.index_entry(
        value,
        name="example",
        host="example-linux",
        started=1000,
        ended=None if rc is None else 2000,
        rc=rc,
        workspace=workspace if prepared else None,
    ) == {
        "v": 2,
        "kind": "exec",
        "harness": "claude",
        "model": value.model,
        "account": "agent",
        "repo": value.repo,
        "class": "example-class",
        "variant": "example-variant",
        "reason": "example reason",
        "role": "worker",
        "unsandboxed": False,
        "authorized_by": None,
        "run": "example",
        "host": "example-linux",
        "worktree": workspace.worktree if prepared else None,
        "started": 1000,
        "ended": None if rc is None else 2000,
        "rc": rc,
        "cleaned": None,
        "kept": False,
        "claude_profile": "secondary" if prepared else "primary",
        "trace_id": None,
        "state": "running" if rc is None else "ended",
        "gh_fallback": prepared,
    }


def test_agy_project() -> None:
    assert json.loads(
        decisions.agy_project(
            "example", "/example/worktree", ("read_file(/example)",), 900
        )
    ) == {
        "name": "agent-example",
        "path": "/example/worktree",
        "permissions": ["read_file(/example)"],
        "timeout": 900,
    }


@pytest.mark.parametrize("harness", Harness)
@pytest.mark.parametrize("kind", RunKind)
def test_host_arguments(harness: Harness, kind: RunKind) -> None:
    args = decisions.host_arguments(
        harness,
        kind,
        (harness.value, "--flag", "last"),
        "/host/tool",
        "/example/out.final",
    )
    if harness is Harness.CODEX and kind is RunKind.EXEC:
        assert args == (
            "/host/tool",
            "--flag",
            "--output-last-message",
            "/example/out.final",
            "last",
        )
    else:
        assert args == ("/host/tool", "--flag", "last")


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ((), "argv: "),
        (
            ("shell_environment_policy.set.EXAMPLE=one",),
            "argv: shell_environment_policy.set.EXAMPLE=one",
        ),
        (("-c", "shell_environment_policy.set.EXAMPLE=one"), "argv: "),
        (
            ("codex", "shell_environment_policy.set.EXAMPLE=one"),
            "argv: codex shell_environment_policy.set.EXAMPLE=one",
        ),
        (
            (
                "-c",
                "-c",
                "shell_environment_policy.set.EXAMPLE=one",
                "shell_environment_policy.set.EXAMPLE=two",
            ),
            "argv: -c shell_environment_policy.set.EXAMPLE=two",
        ),
    ],
)
def test_argv_log_line_drops_only_environment_override_pairs(
    command: tuple[str, ...], expected: str
) -> None:
    assert decisions.argv_log_line(command) == expected


def test_argv_log_line_keeps_other_overrides() -> None:
    command = (
        "codex",
        "-c",
        "shell_environment_policy.set.HOME='x y'",
        "-c",
        "model_reasoning_effort='high'",
        "-c",
        "shell_environment_policy.set.PATH='p'",
        "-m",
        "-c",
    )
    assert decisions.argv_log_line(command) == (
        "argv: codex -c 'model_reasoning_effort='\"'\"'high'\"'\"'' -m -c"
    )


@pytest.mark.parametrize("scheme", ["http", "https"])
def test_resource_attributes_name_host_account_and_run(scheme: str) -> None:
    assert decisions.resource_attributes(
        f"{scheme}://otlp.example.net", "example-linux", "agent", "example"
    ) == {
        "OTEL_RESOURCE_ATTRIBUTES": (
            "host.name=example-linux,tbhb.account=agent,tbhb.run.id=example"
        )
    }


def test_resource_attributes_need_an_endpoint() -> None:
    assert (
        decisions.resource_attributes(None, "example-linux", "agent", "example") == {}
    )

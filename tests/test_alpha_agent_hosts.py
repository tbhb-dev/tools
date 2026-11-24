"""Host selection through the CLI with fake transport and real admission leases."""

from __future__ import annotations

import json
from dataclasses import replace
from typing import TYPE_CHECKING, cast
from unittest.mock import Mock

import pytest
import tomli_w
from agent_driver_example import execution_arguments
from agent_example import LINUX, settings
from test_alpha_agent_config import GOOD

from tbhbdev import userconfig, version
from tbhbdev.alpha import agent
from tbhbdev.alpha.agent import admission, driver, launcher, transport
from tbhbdev.alpha.agent.decisions import remote
from tbhbdev.alpha.agent.model import Invalid, Profile

if TYPE_CHECKING:
    from pathlib import Path

    from tbhbdev.alpha.agent.model import Host

pytestmark = pytest.mark.usefixtures("isolated_home")  # noqa: V107
PROG = "agent"
FREE = "0 0"
HEALTHY = "0 8 16777216 16777216 0 0"
BUSY = "8 8 16777216 16777216 0 0"


def host(index: int, *, eligible: bool = True, limit: int = 3) -> Host:
    return replace(
        LINUX,
        name=f"review-host-{index}",
        auto_build=eligible,
        max_concurrent_runs=limit,
    )


@pytest.fixture
def run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(launcher, "session", lambda: (None, None))
    monkeypatch.setattr(driver, "launch_trust", Mock())
    monkeypatch.setattr(driver, "poll_launch", Mock(return_value=0))
    monkeypatch.setattr(transport, "call", Mock())
    prompt = tmp_path / "prompt"
    prompt.write_text("Build the change.")
    return prompt


def launch(prompt: Path, *options: str, target: str = "auto") -> int:
    return agent.main(execution_arguments(prompt, "--host", target, *options), PROG)


@pytest.mark.parametrize(
    ("options", "account"), [((), LINUX.account), (("--ops",), "ops")]
)
def test_picks_first_free_host(
    run: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    options: tuple[str, ...],
    account: str,
) -> None:
    picked = replace(host(2), tbhbdev="/opt/review host/tbhbdev")
    config = replace(settings(), hosts=(host(1), picked, host(3)))
    monkeypatch.setattr(agent.settings, "load", lambda: config)
    shell = Mock(side_effect=["2 1", FREE, HEALTHY, version.line(), ""])
    monkeypatch.setattr(transport, "shell", shell)
    assert launch(run, *options) == 0
    lines = [
        f"host auto: read {userconfig.path()}",
        "review-host-1: skipped (no agent slot available (max_concurrent_runs=3))",
        "review-host-2: picked",
        "review-host-3: not probed",
    ]
    assert capsys.readouterr().out.splitlines() == lines
    assert [call.args[1].name for call in shell.call_args_list] == [
        "review-host-1",
        *["review-host-2"] * 4,
    ]
    assert all(call.args[1].account == account for call in shell.call_args_list)
    assert (
        "'/opt/review host/tbhbdev' alpha agent slot-counts"
        in shell.call_args_list[1].args[2]
    )
    assert shell.call_args_list[2].args[2] == remote.limits()
    record = json.loads(driver.metadata_path("sample").read_text())
    assert "host_selection" not in record


def test_refusal_lists_every_reason_without_creating_state(
    run: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    configured = (
        host(1, eligible=False),
        host(2, limit=0),
        replace(host(3), accounts=("agent",)),
        host(4),
        host(5),
        host(6),
        host(7),
        host(8),
    )
    monkeypatch.setattr(
        agent.settings, "load", lambda: replace(settings(), hosts=configured)
    )
    shell = Mock(
        side_effect=[
            "2 0",
            FREE,
            BUSY,
            "bad",
            "-1 0",
            transport.TransportError("offline"),
        ]
    )
    monkeypatch.setattr(transport, "shell", shell)
    assert launch(run, "--ops") == 1
    output = capsys.readouterr()
    assert output.out.splitlines() == [
        f"host auto: read {userconfig.path()}",
        "review-host-1: skipped (auto_build is not set)",
        "review-host-2: skipped (max_concurrent_runs is 0)",
        (
            "review-host-3: skipped (account 'ops' isn't one of the accounts of "
            "review-host-3)"
        ),
        "review-host-4: skipped (remaining agent capacity is reserved for reviews)",
        (
            "review-host-5: skipped (review-host-5: load 8 >= limit 8; "
            "pass --ignore-limits to launch anyway)"
        ),
        "review-host-6: skipped (cannot read slot counts: invalid output)",
        "review-host-7: skipped (cannot read slot counts: invalid output)",
        "review-host-8: skipped (cannot read slot counts: offline)",
    ]
    assert "no build host available" in output.err
    assert not driver.directory().exists()
    assert shell.call_count == 6


@pytest.mark.parametrize(
    ("options", "reason"),
    [
        (("--profile", "review"), "requires --profile build"),
        (("--ignore-limits",), "cannot be combined with --ignore-limits"),
    ],
)
def test_auto_refuses_incompatible_flags(
    run: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    options: tuple[str, ...],
    reason: str,
) -> None:
    monkeypatch.setattr(agent.settings, "load", settings)
    shell = Mock()
    monkeypatch.setattr(transport, "shell", shell)
    assert launch(run, *options) == 1
    assert f"--host auto {reason}" in capsys.readouterr().err
    shell.assert_not_called()


def table(index: int, **keys: object) -> dict[str, object]:
    example = cast("list[dict[str, object]]", GOOD["host"])[0]
    return {**example, "name": f"review-host-{index}", **keys}


def write_config(*hosts: dict[str, object]) -> Path:
    path = userconfig.path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(tomli_w.dumps({"agent": GOOD | {"host": list(hosts)}}))
    return path


def test_auto_build_config(capsys: pytest.CaptureFixture[str]) -> None:
    write_config(table(1), table(2, auto_build=True))
    assert [host.auto_build for host in agent.settings.load().hosts] == [False, True]
    write_config(table(1, auto_build="true"))
    assert agent.main([], PROG) == 1
    assert "auto_build must be true or false" in capsys.readouterr().err


def test_slot_probe_matches_admission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "admission"
    monkeypatch.setattr(admission, "ROOT", root)
    assert agent.main(["slot-counts"], PROG) == 0
    assert capsys.readouterr().out == "0 0\n"
    assert not root.exists()
    target = host(1, limit=2)
    with admission.reserve(root, target, Profile.BUILD, 2):
        assert agent.main(["slot-counts"], PROG) == 0
        shell = Mock(return_value=capsys.readouterr().out)
        monkeypatch.setattr(transport, "shell", shell)
        assert driver.probe_build_host(settings(), target) == (
            "remaining agent capacity is reserved for reviews"
        )
        with (
            pytest.raises(
                Invalid, match="remaining agent capacity is reserved for reviews"
            ),
            admission.reserve(root, target, Profile.BUILD, 2),
        ):
            pytest.fail("build claimed the review slot")
        with admission.reserve(root, target, Profile.REVIEW, 2):
            assert admission.counts(root) == (1, 1)
    assert admission.counts(root) == (0, 0)
    (root / "build-invalid").mkdir()
    assert agent.main(["slot-counts"], PROG) == 1
    assert "slot must be a regular file" in capsys.readouterr().err

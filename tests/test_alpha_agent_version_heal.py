"""Launch and synchronize versions with injected Git, SSH, uv and exec calls."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from typing import TYPE_CHECKING
from unittest.mock import Mock

import pytest
from agent_driver_example import execution_arguments
from agent_example import settings
from agent_host_example import commit, git

from tbhbdev import version
from tbhbdev.alpha import agent, version_check
from tbhbdev.alpha.agent import driver, transport, version_heal
from tbhbdev.alpha.agent.decisions import remote
from tbhbdev.alpha.agent.model import Invalid

if TYPE_CHECKING:
    from pathlib import Path

CONFIG = settings()
HOST = CONFIG.hosts[0]
HEAD = "a" * 40
OLD = "b" * 40
NEW_LINE = f"tbhbdev 0 ({HEAD})"
OLD_LINE = f"tbhbdev 0 ({OLD})"
URL = "https://github.com/tbhb-dev/tools"
REF = "refs/heads/main"
GIT_OVERRIDES = ("-c", "credential.helper=", "-c", "http.extraHeader=")
GIT = ["git", *GIT_OVERRIDES, "ls-remote", URL, REF]
INSTALL = ("uv", "tool", "install", "--no-config", "--force", f"git+{URL}@{HEAD}")
RETRY = "_TBHB_AGENT_VERSION_HEAL"
ORIGIN = subprocess.CompletedProcess(GIT, 0, f"{HEAD}\t{REF}\n")


@pytest.fixture(autouse=True)
def fresh_launch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(RETRY, raising=False)


@pytest.mark.parametrize("scope", ["--local", "--global"])
def test_read_origin_ignores_git_url_rewrites(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch, scope: str
) -> None:
    env = {"PATH": os.environ["PATH"], "GIT_CONFIG_NOSYSTEM": "1"}
    git(isolated_home, "init", "--template=", "--initial-branch=main", env=env)
    fake_head = commit(isolated_home, "fake main", env=env)
    key = f"url.{isolated_home.as_uri()}.insteadOf"
    git(isolated_home, "config", scope, key, URL, env=env)
    # Disable real HTTPS while still allowing a rewrite to the test repository.
    command = ["-c", "protocol.https.allow=never", "ls-remote", URL, REF]
    assert git(isolated_home, *command, env=env) == f"{fake_head}\t{REF}"
    monkeypatch.chdir(isolated_home)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(isolated_home / ".gitconfig"))
    status, output = version_heal.read_origin(["git", *command])
    assert status != 0
    assert output == ""


def test_read_origin_uses_fixed_url_and_clean_git_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GIT_DIR", "/example/.git")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "credential.helper")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "example")
    run = Mock(return_value=ORIGIN)
    monkeypatch.setattr(subprocess, "run", run)

    assert version_check.latest(version_heal.read_origin) == HEAD
    run.assert_called_once()
    assert run.call_args.args == (GIT,)
    assert run.call_args.kwargs["cwd"] == "/"
    assert run.call_args.kwargs["env"] == {
        "PATH": os.environ["PATH"],
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ASKPASS": "/bin/false",
    }


def test_versions_match_without_sync(monkeypatch: pytest.MonkeyPatch) -> None:
    unexpected = Mock(side_effect=AssertionError("unexpected sync"))
    monkeypatch.setattr(subprocess, "run", unexpected)
    monkeypatch.setattr(transport, "shell", unexpected)
    assert version_heal.check(CONFIG, HOST, OLD_LINE, OLD_LINE) is None
    unexpected.assert_not_called()


@pytest.mark.parametrize("host_after", [NEW_LINE, f"tbhbdev 0 ({'c' * 40})"])
@pytest.mark.usefixtures("isolated_home")
def test_host_sync_retries_review_preflight(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    host_after: str,
) -> None:
    run = Mock(return_value=ORIGIN)
    shell = Mock(
        side_effect=[
            "0 4 12582912 6291456 0 0",
            OLD_LINE,
            "",
            f"{host_after}\n{HEAD}\trefs/heads/topic\n",
        ]
    )
    stream = Mock()
    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(version, "line", lambda: NEW_LINE)
    monkeypatch.setattr(transport, "shell", shell)
    monkeypatch.setattr(transport, "stream", stream)
    monkeypatch.setattr(transport, "call", Mock())
    monkeypatch.setattr(driver, "poll_launch", Mock(return_value=0))
    monkeypatch.setattr(agent.settings, "load", lambda: CONFIG)
    prompt = tmp_path / "brief.md"
    prompt.write_text("Review the example.\n")
    argv = execution_arguments(prompt, "--profile", "review", "--ref", "topic")
    assert agent.main(argv, "example agent") == (0 if host_after == NEW_LINE else 1)
    run.assert_called_once()
    assert run.call_args.args == (GIT,)
    assert run.call_args.kwargs["env"]["GIT_TERMINAL_PROMPT"] == "0"
    _, preflight, install, reread = [call.args for call in shell.call_args_list]
    assert preflight == reread
    assert install == (CONFIG, HOST, remote.PREFIX + "exec " + " ".join(INSTALL))
    err = capsys.readouterr().err
    assert err.splitlines()[0] == f"version heal: host {HOST.name} {OLD}->{HEAD}"
    if host_after == NEW_LINE:
        stream.assert_called_once()
        assert driver.load(CONFIG, "sample").request.commit == HEAD
    else:
        stream.assert_not_called()
        assert (
            f"version mismatch: driver {NEW_LINE!r}; host {OLD_LINE!r} (after one sync)"
            in err
        )


@pytest.mark.parametrize("installed", [NEW_LINE, "tbhbdev 0 (unknown)"])
def test_driver_sync_execs_the_same_argv_once(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], installed: str
) -> None:
    argv = ["/example/bin/tbhbdev", "alpha", "agent", "exec", "sample", "brief.md"]
    original = f"version mismatch: driver {OLD_LINE!r}; host {installed!r}"
    run = Mock(side_effect=[ORIGIN, subprocess.CompletedProcess(INSTALL, 0)])
    shell = Mock()

    def execv(path: str, args: list[str]) -> None:
        assert path == argv[0]
        assert args == argv
        assert os.environ[RETRY] == original
        with pytest.raises(Invalid) as caught:
            version_heal.check(CONFIG, HOST, NEW_LINE, OLD_LINE)
        assert str(caught.value) == f"{original} (after one sync)"
        raise SystemExit(1)

    restart = Mock(side_effect=execv)
    monkeypatch.setattr(sys, "argv", argv)
    monkeypatch.setattr(os, "execv", restart)
    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(transport, "shell", shell)
    with pytest.raises(SystemExit):
        version_heal.check(CONFIG, HOST, OLD_LINE, installed)
    restart.assert_called_once()
    assert [call.args[0] for call in run.call_args_list] == [GIT, INSTALL]
    assert run.call_args.kwargs["check"] is True
    assert run.call_args.kwargs["stderr"] == subprocess.DEVNULL
    assert RETRY not in os.environ
    log = capsys.readouterr().err.splitlines()
    assert log[-1] == f"version heal: driver {OLD}->{HEAD}"
    if installed == NEW_LINE:
        assert len(log) == 1
        shell.assert_not_called()
    else:
        assert log[0] == f"version heal: host {HOST.name} unknown->{HEAD}"
        shell.assert_called_once()


def test_matching_retry_consumes_marker(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(RETRY, "original mismatch")
    assert version_heal.check(CONFIG, HOST, NEW_LINE, NEW_LINE) is None
    assert RETRY not in os.environ


def test_empty_brief_is_refused_after_driver_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prompt = tmp_path / "brief.md"
    prompt.touch()
    args = argparse.Namespace(
        name="sample", prompt_file=prompt, fetch_ref=[], profile="build", start_ref=None
    )
    assert driver.checked_prompt(args) == ""
    monkeypatch.setenv(RETRY, "original mismatch")
    with pytest.raises(Invalid, match="brief is empty after the driver restarted"):
        driver.checked_prompt(args)


def test_module_restart_replays_python_argv(monkeypatch: pytest.MonkeyPatch) -> None:
    argv = ["python", "-m", "tbhbdev", "alpha", "agent", "exec"]
    monkeypatch.setattr(sys, "orig_argv", argv)
    monkeypatch.setattr(sys, "argv", ["/example/tbhbdev/__main__.py", *argv[3:]])
    restart = Mock(side_effect=FileNotFoundError("Python missing"))
    monkeypatch.setattr(os, "execv", restart)
    with pytest.raises(FileNotFoundError, match="Python missing"):
        version_heal.restart("original mismatch")
    restart.assert_called_once_with(sys.executable, argv)
    assert RETRY not in os.environ


@pytest.mark.parametrize(
    ("local", "installed", "failure"),
    [
        (OLD_LINE, NEW_LINE, FileNotFoundError("uv missing")),
        (NEW_LINE, OLD_LINE, transport.TransportError("SSH failed")),
    ],
)
def test_sync_failure_preserves_the_mismatch(
    monkeypatch: pytest.MonkeyPatch, local: str, installed: str, failure: OSError
) -> None:
    monkeypatch.setattr(subprocess, "run", Mock(side_effect=[ORIGIN, failure]))
    monkeypatch.setattr(transport, "shell", Mock(side_effect=failure))
    with pytest.raises(Invalid) as caught:
        version_heal.check(CONFIG, HOST, local, installed)
    assert str(caught.value) == (
        f"version mismatch: driver {local!r}; host {installed!r}"
        f" (version sync failed: {failure})"
    )


@pytest.mark.parametrize(
    ("origin", "installed"),
    [
        (OSError("Git missing"), OLD_LINE),
        (subprocess.CompletedProcess(GIT, 1, ""), OLD_LINE),
        (subprocess.CompletedProcess(GIT, 0, "garbled"), OLD_LINE),
        (ORIGIN, f"tbhbdev 1 ({HEAD})"),
    ],
)
def test_no_install_preserves_the_mismatch(
    monkeypatch: pytest.MonkeyPatch, origin: object, installed: str
) -> None:
    run = Mock(side_effect=[origin])
    shell = Mock()
    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(transport, "shell", shell)
    with pytest.raises(Invalid) as caught:
        version_heal.check(CONFIG, HOST, NEW_LINE, installed)
    assert (
        str(caught.value)
        == f"version mismatch: driver {NEW_LINE!r}; host {installed!r}"
    )
    run.assert_called_once()
    shell.assert_not_called()

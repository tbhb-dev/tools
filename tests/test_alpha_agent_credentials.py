"""Fake Connect reads, private destinations and a disposable signing agent."""

from __future__ import annotations

import dataclasses
import json
import subprocess
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest
import yaml
from agent_host_example import host_settings, run_request

from tbhbdev import secrets
from tbhbdev.alpha import gh_app
from tbhbdev.alpha.agent import credentials, process_group, runfiles
from tbhbdev.alpha.agent.model import Harness, Invalid, Profile

if TYPE_CHECKING:
    from collections.abc import Generator


VALUES = {
    "example/gh/value": "example-gh-credential",
    "example/docker/value": (
        '{"auths":{"example.com":{"auth":"example-docker-credential"}}}'
    ),
    "example/signing/key": "example-signing",
    "example/primary/token": "example-claude",
    "example/app/id": "123",
    "example/app/client": "example-client",
}


@pytest.fixture(autouse=True)
def fake_reader(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []

    def read(reference: str) -> str:
        calls.append(reference)
        return VALUES[reference]

    monkeypatch.setattr(secrets, "read", read)
    return calls


@pytest.mark.parametrize("harness", Harness)
@pytest.mark.parametrize("profile", Profile)
def test_each_credential_only_reaches_its_destination(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_reader: list[str],
    harness: Harness,
    profile: Profile,
) -> None:
    def installation(actual: object, repository: str) -> str:
        assert actual == host_settings().credentials[0]
        assert repository == value.repo
        return "example-installation"

    monkeypatch.setattr(credentials, "installation_token", installation)
    value = run_request(
        harness=harness.value,
        profile=profile.value,
        commit="0" * 40 if profile is Profile.REVIEW else None,
        claude_profile="primary" if harness is Harness.CLAUDE else None,
        reason="example reason" if harness is Harness.CLAUDE else None,
        timeout=900,
    )
    result = credentials.deliver(value, host_settings(), tmp_path)
    assert result.gh_fallback is False
    assert result.signing == (
        VALUES["example/signing/key"] if profile is Profile.BUILD else None
    )
    assert result.claude_profile == ("primary" if harness is Harness.CLAUDE else None)
    assert result.env == (
        {credentials.CLAUDE_TOKEN_ENV: VALUES["example/primary/token"]}
        if harness is Harness.CLAUDE
        else {}
    )
    gh = yaml.safe_load((tmp_path / "gh" / "hosts.yml").read_text())
    assert gh == {
        "github.com": {
            "oauth_token": "example-installation"
            if profile is Profile.REVIEW
            else VALUES["example/gh/value"],
            "git_protocol": "https",
        }
    }
    assert (tmp_path / "docker" / "config.json").read_text() == VALUES[
        "example/docker/value"
    ]
    for directory, filename in (("gh", "hosts.yml"), ("docker", "config.json")):
        assert (tmp_path / directory).stat().st_mode & 0o777 == 0o700
        assert (tmp_path / directory / filename).stat().st_mode & 0o777 == 0o600
    for marker in VALUES.values():
        assert marker not in repr(result)
    expected = ["example/gh/value"] if profile is Profile.BUILD else []
    expected += ["example/docker/value"]
    if harness is Harness.CLAUDE:
        expected.append("example/primary/token")
    if profile is Profile.BUILD:
        expected.append("example/signing/key")
    assert fake_reader == expected


def test_review_falls_back_and_anonymous_docker_reads_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_reader: list[str]
) -> None:
    def fail(*_: object) -> str:
        message = "unprinted-app-material"
        raise gh_app.GhAppError(message)

    monkeypatch.setattr(credentials, "installation_token", fail)
    original = host_settings()
    config = dataclasses.replace(
        original,
        credentials=(dataclasses.replace(original.credentials[0], docker=None),),
    )
    result = credentials.deliver(
        run_request(profile="review", commit="0" * 40), config, tmp_path
    )
    assert result.gh_fallback is True
    assert result.signing is None
    assert fake_reader == ["example/gh/value"]
    assert json.loads((tmp_path / "docker" / "config.json").read_text()) == {
        "auths": {}
    }


@pytest.mark.parametrize("value", ["", " ", "\n\t", None])
def test_claude_empty_or_failed_read_names_profile_and_reference(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: str | None
) -> None:
    def read(reference: str) -> str:
        if reference != "example/primary/token":
            return VALUES[reference]
        if value is None:
            message = "marker-value-that-must-not-escape"
            raise secrets.SecretsError(message)
        return value

    monkeypatch.setattr(secrets, "read", read)
    with pytest.raises(Invalid) as caught:
        credentials.deliver(
            run_request(harness="claude", claude_profile="primary", reason="example"),
            host_settings(),
            tmp_path,
        )
    assert (
        str(caught.value) == "claude profile primary cannot read credential reference "
        "example/primary/token"
    )
    assert "marker-value" not in str(caught.value)


def test_unknown_claude_profile(tmp_path: Path) -> None:
    with pytest.raises(Invalid, match=r"^claude profile 'unknown' is not configured$"):
        credentials.deliver(
            run_request(harness="claude", claude_profile="unknown", reason="example"),
            host_settings(),
            tmp_path,
        )


@pytest.mark.parametrize("failure", [None, "host", "sign", "request"])
def test_installation_reuses_app_signer_without_storing_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str | None,
    fake_reader: list[str],
) -> None:
    pem = "example-pem"
    monkeypatch.setitem(VALUES, "example/app/pem", pem)
    env = gh_app.Env(tmp_path, lambda: 1000, secrets.Store, gh_app.run_openssl)
    monkeypatch.setattr(gh_app, "default_env", lambda: env)
    calls: list[str] = []

    def sign(actual: gh_app.Env, client_id: str, key: str) -> str:
        assert actual is env
        assert client_id == "example-client"
        assert key == pem
        calls.append("sign")
        if failure == "sign":
            message = "signing failed"
            raise gh_app.GhAppError(message)
        return "example-jwt"

    def token(actual: gh_app.Env, args: gh_app.Args, jwt: str) -> gh_app.Minted:
        assert actual is env
        assert args == gh_app.Args(
            app="reviews",
            repo="example-org/example-repo",
            fresh=True,
            dry_run=False,
            all_repos=False,
            vault="",
            cooldown_until=None,
            cooldown_status=False,
        )
        assert jwt == "example-jwt"
        calls.append("request")
        if failure == "request":
            message = "request failed"
            raise gh_app.GhAppError(message)
        return gh_app.Minted("example-installation", 2000)

    monkeypatch.setattr(gh_app, "sign", sign)
    monkeypatch.setattr(gh_app, "request_token", token)
    repository = (
        "example.com/example-org/example-repo"
        if failure == "host"
        else "example-org/example-repo"
    )
    if failure:
        with pytest.raises(
            Invalid if failure == "host" else gh_app.GhAppError
        ) as caught:
            credentials.installation_token(host_settings().credentials[0], repository)
        if failure == "host":
            assert str(caught.value) == "reviews app supports github.com repositories"
    else:
        assert (
            credentials.installation_token(host_settings().credentials[0], repository)
            == "example-installation"
        )
    assert calls == (
        []
        if failure == "host"
        else ["sign"]
        if failure == "sign"
        else ["sign", "request"]
    )
    assert fake_reader == (
        [] if failure == "host" else ["example/app/client", "example/app/pem"]
    )
    assert {p.name for p in tmp_path.iterdir()} == {"xdg"}


def test_signing_key_is_loaded_over_stdin_and_agent_stops(tmp_path: Path) -> None:
    key = tmp_path / "disposable-key"
    subprocess.run(
        [
            "ssh-keygen",
            "-q",
            "-t",
            "ed25519",
            "-N",
            "",
            "-C",
            "example",
            "-f",
            str(key),
        ],
        check=True,
        timeout=10,
    )
    with tempfile.TemporaryDirectory(prefix="ag-test-") as directory:
        tmpdir = Path(directory)
        agent = credentials.signing_agent(key.read_text(), tmpdir, time.monotonic() + 5)
        try:
            env = {"SSH_AUTH_SOCK": str(tmpdir / "ssh.sock")}
            result = subprocess.check_output(
                ["ssh-add", "-L"], env=env, text=True, timeout=5
            )
            assert result.strip() == key.with_suffix(".pub").read_text().strip()
            assert agent.poll() is None
            assert [p.name for p in tmpdir.iterdir()] == ["ssh.sock"]
        finally:
            process_group.stop_started(agent.pid, 0)
            agent.wait(timeout=5)
        assert agent.returncode is not None


@pytest.mark.parametrize("exited", [False, True])
def test_signing_agent_start_failure_is_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, exited: bool
) -> None:
    class Agent:
        pid = 23

        def poll(self) -> int | None:
            return 1 if exited else None

        def wait(self) -> int:
            return 1

    agent = Agent()

    def start(*_args: object, **_kwargs: object) -> Agent:
        return agent

    monkeypatch.setattr(subprocess, "Popen", start)
    stops: list[tuple[int, float]] = []

    def stop(pid: int, grace: float) -> None:
        stops.append((pid, grace))

    monkeypatch.setattr(process_group, "stop_started", stop)
    with pytest.raises(Invalid if exited else TimeoutError) as caught:
        credentials.signing_agent("example-key", tmp_path, time.monotonic() - 1)
    assert str(caught.value) == ("run signing agent could not start" if exited else "")
    assert stops == [(23, 0)]


@pytest.mark.parametrize(
    "state",
    [
        (None, None, None, None),
        ("old", None, "old", "old"),
        ("old", "old", "old", "old"),
        ("old", "new", "old", "new"),
        ("old", "new", "newer", "newer"),
        (None, "new", None, "new"),
    ],
)
def test_auth_copy_and_write_back_under_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    state: tuple[str | None, str | None, str | None, str | None],
) -> None:
    before, after, current, wanted = state
    locks: list[Path] = []
    real_lock = runfiles.locked

    @contextmanager
    def locked(path: Path) -> Generator[None]:
        locks.append(path)
        with real_lock(path):
            yield

    monkeypatch.setattr(runfiles, "locked", locked)
    source_dir = runfiles.private_dir(tmp_path / "source")
    dest_dir = runfiles.private_dir(tmp_path / "dest")
    source, destination = source_dir / "auth.json", dest_dir / "auth.json"
    if before is not None:
        runfiles.atomic(source, before)
    assert credentials.auth_copy(source, destination) == before
    if before is not None:
        assert destination.read_text() == before
        assert destination.stat().st_mode & 0o777 == 0o600
    if after is None and destination.exists():
        destination.rename(dest_dir / "saved")
    elif after is not None:
        runfiles.atomic(destination, after)
    if current is not None:
        runfiles.atomic(source, current)
    credentials.auth_write_back(source, destination, before)
    assert (source.read_text() if source.exists() else None) == wanted
    assert (source_dir / "agent-auth.lock").exists()
    assert locks == [source_dir / "agent-auth.lock"] * (
        2 if after is not None and before != after else 1
    )


@pytest.mark.parametrize("failed", [False, True])
@pytest.mark.parametrize("remaining", [-1, 8])
def test_signing_delivery_uses_private_stdin_and_a_dedicated_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failed: bool, remaining: int
) -> None:
    events: list[str] = []
    key = "example-key"
    socket = tmp_path / "ssh.sock"

    class Agent:
        pid = 23

        def wait(self) -> int:
            events.append("wait")
            return 0

    agent = Agent()

    def spawn(command: list[str], **kwargs: object) -> Agent:
        assert command == ["ssh-agent", "-D", "-a", str(socket)]
        assert kwargs == {
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
            "start_new_session": True,
            "text": True,
        }
        events.append("spawn")
        return agent

    def ready(actual: object, path: Path, deadline: float) -> None:
        assert actual is agent
        assert path == socket
        assert deadline == 10
        events.append("ready")

    def add(command: list[str], **kwargs: object) -> None:
        assert command == ["ssh-add", "-"]
        assert kwargs == {
            "input": key,
            "env": {"SSH_AUTH_SOCK": str(socket)},
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
            "text": True,
            "check": True,
            "timeout": max(0, remaining),
        }
        events.append("add")
        if failed:
            raise subprocess.CalledProcessError(1, command)

    def stop(group: int, grace: float) -> None:
        assert (group, grace) == (23, 0)
        events.append("stop")

    monkeypatch.setattr(subprocess, "Popen", spawn)
    monkeypatch.setattr(credentials, "agent_ready", ready)
    monkeypatch.setattr(subprocess, "run", add)
    monkeypatch.setattr(time, "monotonic", lambda: 10 - remaining)
    monkeypatch.setattr(process_group, "stop_started", stop)
    if failed:
        with pytest.raises(subprocess.CalledProcessError):
            credentials.signing_agent(key, tmp_path, 10)
    else:
        assert credentials.signing_agent(key, tmp_path, 10) is agent
    assert events == ["spawn", "ready", "add", *(["stop", "wait"] if failed else [])]


@pytest.mark.parametrize("expired", [False, True])
def test_signing_socket_wait_obeys_deadline_and_interval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, expired: bool
) -> None:
    socket = tmp_path / "ssh.sock"
    events: list[float] = []

    class Agent:
        def poll(self) -> None:
            return None

    agent = Agent()

    def pause(duration: float) -> None:
        assert duration == 0.01
        events.append(duration)
        socket.touch()

    monkeypatch.setattr(time, "monotonic", lambda: 10 if expired else 2)
    monkeypatch.setattr(time, "sleep", pause)
    if expired:
        with pytest.raises(TimeoutError):
            credentials.agent_ready(cast("subprocess.Popen[str]", agent), socket, 10)
    else:
        credentials.agent_ready(cast("subprocess.Popen[str]", agent), socket, 10)
    assert events == ([] if expired else [0.01])


@pytest.mark.parametrize(
    ("reference", "label", "profile"),
    [
        ("example/gh/value", "gh", "build"),
        ("example/gh/value", "gh", "review"),
        ("example/docker/value", "docker", "build"),
        ("example/signing/key", "signing", "build"),
        ("example/app/client", "reviews app", "app"),
        ("example/app/pem", "reviews app", "app"),
    ],
)
def test_missing_credentials_report_only_label_and_reference(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    reference: str,
    label: str,
    profile: str,
) -> None:
    def read(actual: str) -> str:
        return "" if actual == reference else VALUES[actual]

    def installation(*_args: object) -> str:
        message = "unprinted-marker"
        raise gh_app.GhAppError(message)

    monkeypatch.setattr(secrets, "read", read)
    if profile != "app":
        monkeypatch.setattr(credentials, "installation_token", installation)

    def perform() -> object:
        if profile == "app":
            return credentials.installation_token(
                host_settings().credentials[0], "example-org/example-repo"
            )
        return credentials.deliver(
            run_request(
                profile=profile, commit="0" * 40 if profile == "review" else None
            ),
            host_settings(),
            tmp_path,
        )

    with pytest.raises(Invalid) as caught:
        perform()
    assert str(caught.value) == f"{label} cannot read credential reference {reference}"

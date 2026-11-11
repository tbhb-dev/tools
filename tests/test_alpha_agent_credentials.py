"""Fake Connect reads, private destinations and a disposable signing agent."""

from __future__ import annotations

import dataclasses
import json
import os
import signal
import subprocess
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, cast

import httpx
import pytest
import yaml
from agent_host_example import constant, git, host_settings, repository, run_request
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from tbhbdev import secrets
from tbhbdev.alpha import gh_app
from tbhbdev.alpha.agent import credentials, process_group, runfiles
from tbhbdev.alpha.agent.model import Harness, Invalid, Profile

if TYPE_CHECKING:
    from collections.abc import Callable, Generator
    from typing import Literal


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
    assert {"gh", "docker"} <= {p.name for p in tmp_path.iterdir()}
    for directory, filename in (("gh", "hosts.yml"), ("docker", "config.json")):
        assert {p.name for p in (tmp_path / directory).iterdir()} == {filename}
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


@pytest.mark.parametrize(
    "failure",
    [
        Invalid,
        OSError,
        ValueError,
        KeyError,
        TypeError,
        httpx.HTTPError,
        gh_app.GhAppError,
    ],
)
def test_review_token_failure_never_delivers_the_broad_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_reader: list[str],
    failure: type[Exception],
) -> None:
    def fail(*_: object) -> str:
        message = "unprinted-app-material"
        raise failure(message)

    monkeypatch.setattr(credentials, "installation_token", fail)
    with pytest.raises(Invalid) as caught:
        credentials.deliver(
            run_request(profile="review", commit="0" * 40), host_settings(), tmp_path
        )
    assert str(caught.value) == "reviews App token unavailable; review refused"
    assert fake_reader == []
    assert {p.name for p in tmp_path.iterdir()} == {"xdg"}


def test_anonymous_docker_reads_no_docker_credential(
    tmp_path: Path, fake_reader: list[str]
) -> None:
    original = host_settings()
    config = dataclasses.replace(
        original,
        credentials=(dataclasses.replace(original.credentials[0], docker=None),),
    )
    result = credentials.deliver(run_request(), config, tmp_path)
    assert result.gh_fallback is False
    assert fake_reader == ["example/gh/value", "example/signing/key"]
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
        assert dataclasses.asdict(args) == {
            "app": "reviews",
            "repo": "example-org/example-repo",
            "fresh": True,
            "dry_run": False,
            "all_repos": False,
            "vault": "",
            "cooldown_until": None,
            "cooldown_status": False,
        }
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
    check_installation_calls(calls, fake_reader, failure, tmp_path)


def test_review_token_request_keeps_installed_permissions_for_only_run_repository(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    monkeypatch.setitem(VALUES, "example/app/pem", pem)
    requests: list[httpx.Request] = []
    replies = {
        ("GET", "/repos/example-org/example-repo/installation"): httpx.Response(
            200, json={"id": 7}
        ),
        ("POST", "/app/installations/7/access_tokens"): httpx.Response(
            201,
            json={
                "token": "example-installation",
                "expires_at": "2030-01-01T00:00:00Z",
            },
        ),
    }

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return replies[request.method, request.url.path]

    env = gh_app.Env(
        tmp_path,
        lambda: 1000,
        secrets.Store,
        gh_app.run_openssl,
        httpx.MockTransport(respond),
    )
    monkeypatch.setattr(gh_app, "default_env", lambda: env)
    monkeypatch.setattr(gh_app, "sign", constant("example-jwt"))
    credentials.deliver(
        run_request(profile="review", commit="0" * 40), host_settings(), tmp_path
    )
    assert [(request.method, request.url.path) for request in requests] == list(replies)
    assert json.loads(requests[1].content) == {"repositories": ["example-repo"]}


def check_installation_calls(
    calls: list[str], fake_reader: list[str], failure: str | None, tmp_path: Path
) -> None:
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
                [credentials.host_tools.resolve("ssh-add"), "-L"],
                env=env,
                text=True,
                timeout=5,
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

    def stop(pid: int, grace: float, reap: Callable[[], object] | None = None) -> bool:
        assert reap == agent.poll
        stops.append((pid, grace))
        return True

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
        (None, "new", None, None),
    ],
)
def test_auth_copy_and_write_back_under_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    state: tuple[str | None, str | None, str | None, str | None],
) -> None:
    before, after, current, wanted = (
        json.dumps({"value": value}) if value is not None else None for value in state
    )
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
        2 if before is not None and after is not None and before != after else 1
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

        def poll(self) -> None:
            return None

        def wait(self) -> int:
            events.append("wait")
            return 0

    agent = Agent()

    def spawn(command: list[str], **kwargs: object) -> Agent:
        assert command == [
            credentials.host_tools.resolve("ssh-agent"),
            "-D",
            "-a",
            str(socket),
        ]
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
        assert command == [credentials.host_tools.resolve("ssh-add"), "-"]
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

    def stop(group: int, grace: float, *_args: object) -> None:
        assert (group, grace) == (23, 0)
        assert all(
            signal.getsignal(s) == signal.SIG_IGN for s in process_group.INTERRUPTS
        )
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


def test_signer_cleanup_denial_keeps_original_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Agent:
        pid = 23

        def poll(self) -> None:
            return None

        def wait(self) -> None:
            pytest.fail("an unproven group must not cause an unbounded wait")

    def failed(*_args: object) -> None:
        message = "startup error"
        raise Invalid(message)

    monkeypatch.setattr(subprocess, "Popen", constant(Agent()))
    monkeypatch.setattr(credentials, "agent_ready", failed)
    monkeypatch.setattr(process_group, "stop_started", constant(value=False))
    with pytest.raises(Invalid, match=r"^startup error$"):
        credentials.signing_agent("example-key", tmp_path, 10)


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


@pytest.mark.parametrize(
    "attack",
    ["symlink", "parent", "fifo", "oversized", "json", "array", "owner", "unicode"],
)
def test_auth_write_back_refuses_untrusted_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, attack: str
) -> None:
    source = runfiles.private_dir(tmp_path / "host") / "auth.json"
    source.write_text('{"value":"original"}')
    parent = runfiles.private_dir(tmp_path / "codex")
    destination = parent / "auth.json"
    outside = tmp_path / "outside"
    outside.write_text('{"value":"outside"}')
    if attack == "symlink":
        destination.symlink_to(outside)
    elif attack == "parent":
        parent.rename(tmp_path / "moved")
        parent.symlink_to(tmp_path / "moved", target_is_directory=True)
        destination.write_text('{"value":"outside"}')
    elif attack == "fifo":
        os.mkfifo(destination)
    elif attack == "oversized":
        destination.write_text("{" + " " * (1024 * 1024) + "}")
    elif attack == "owner":
        destination.write_text("{}")
        monkeypatch.setattr(os, "getuid", lambda: destination.stat().st_uid + 1)
    elif attack == "unicode":
        destination.write_bytes(b"\xff")
    else:
        destination.write_text("[1]" if attack == "array" else "invalid")
    reason = credentials.auth_write_back(source, destination, source.read_text())
    assert reason is not None
    expected = {
        "parent": "codex auth parent is redirected",
        "fifo": "codex auth is not an owned regular file",
        "owner": "codex auth is not an owned regular file",
        "oversized": "codex auth exceeds 1 MiB",
        "array": "auth: found array; expected object",
    }.get(attack, "cannot safely read a JSON object")
    assert reason == f"codex auth write-back skipped: {expected}\n"
    assert "outside" not in reason
    assert source.read_text() == '{"value":"original"}'
    assert not (source.parent / "agent-auth.lock").exists()


def test_auth_write_back_accepts_exact_size_limit(tmp_path: Path) -> None:
    source = runfiles.private_dir(tmp_path / "host") / "auth.json"
    destination = runfiles.private_dir(tmp_path / "codex") / "auth.json"
    text = '{"value":"' + "a" * (1024 * 1024 - 12) + '"}'
    assert len(text) == 1024 * 1024
    destination.write_text(text)
    source.write_text('{"value":"original"}')
    assert credentials.auth_write_back(source, destination, source.read_text()) is None
    assert source.read_text() == text


def test_auth_directory_is_pinned_and_reads_are_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = runfiles.private_dir(tmp_path / "codex") / "auth.json"
    destination.write_text("{}")
    opened = os.open
    wrapped = os.fdopen
    sizes: list[int] = []
    flags_seen: list[int] = []

    def checked_open(
        path: str | Path, flags: int, mode: int = 0o777, *, dir_fd: int | None = None
    ) -> int:
        if path == destination.parent:
            assert flags == os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
            flags_seen.append(flags)
        return opened(path, flags, mode, dir_fd=dir_fd)

    @contextmanager
    def checked_file(fd: int, mode: Literal["rb"]) -> Generator[object]:
        with wrapped(fd, mode) as stream:

            class Reader:
                def fileno(self) -> int:
                    return stream.fileno()

                def read(self, size: int) -> bytes:
                    assert size == 1024 * 1024 + 1
                    sizes.append(size)
                    return stream.read(size)

            yield Reader()

    monkeypatch.setattr(os, "open", checked_open)
    monkeypatch.setattr(os, "fdopen", checked_file)
    assert credentials.auth_after(destination) == "{}"
    assert sizes == [1024 * 1024 + 1]
    assert flags_seen == [os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW]


def test_app_token_scope_ignores_planted_origin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_reader: list[str]
) -> None:

    _, clone = repository(tmp_path / "repo")
    git(
        clone,
        "config",
        "remote.origin.url",
        "https://github.com/example-other/other.git",
    )
    env = gh_app.Env(tmp_path, lambda: 1000, secrets.Store, gh_app.run_openssl)
    monkeypatch.setattr(gh_app, "default_env", lambda: env)
    monkeypatch.setattr(gh_app, "rebuild_pem", constant("fake pem"))
    monkeypatch.setattr(gh_app, "sign", constant("fake jwt"))
    monkeypatch.setitem(VALUES, "example/app/pem", "fake pem")
    scopes: list[str] = []

    def token(_env: gh_app.Env, args: gh_app.Args, _jwt: str) -> gh_app.Minted:
        scopes.append(args.repo)
        return gh_app.Minted("fake installation", 2000)

    monkeypatch.setattr(gh_app, "request_token", token)
    value = run_request(profile="review", commit="0" * 40)
    credentials.deliver(value, host_settings(), tmp_path)
    assert scopes == ["example-org/example-repo"]
    assert fake_reader[:2] == ["example/app/client", "example/app/pem"]


@pytest.mark.parametrize(
    ("after", "why"),
    [
        ('{"value":"new","extra":true}', "auth.extra: found boolean; unexpected key"),
        ('{"tokens":[]}', "tokens: found array; expected object"),
        (
            '{"last_refresh":"2025-12-31T00:00:00Z"}',
            "last_refresh: found string; timestamp moved backwards",
        ),
    ],
)
def test_auth_content_refusal_is_logged_without_replacement(
    tmp_path: Path, after: str, why: str
) -> None:
    source = runfiles.private_dir(tmp_path / "host") / "auth.json"
    source.write_text(
        '{"value":"old","tokens":{},"last_refresh":"2026-01-01T00:00:00Z"}'
    )
    # Keep a valid tokens object for the timestamp case.
    data = json.loads(after)
    if "last_refresh" in data:
        data["tokens"] = {
            "id_token": "fixture",
            "access_token": "fixture",
            "refresh_token": "fixture",
        }
    target = runfiles.private_dir(tmp_path / "child") / "auth.json"
    target.write_text(json.dumps(data))
    before = source.read_text()
    assert (
        credentials.auth_write_back(source, target, before)
        == f"codex auth write-back skipped: {why}\n"
    )
    assert source.read_text() == before


def test_empty_auth_can_create_a_missing_source(tmp_path: Path) -> None:
    source = runfiles.private_dir(tmp_path / "host") / "auth.json"
    target = runfiles.private_dir(tmp_path / "child") / "auth.json"
    target.write_text("{}")
    assert credentials.auth_write_back(source, target, None) is None
    assert source.read_text() == "{}"


@pytest.mark.parametrize("missing", ["client", "pem"])
def test_reviews_app_without_configured_references_is_refused(missing: str) -> None:
    references = host_settings().credentials[0]
    references = dataclasses.replace(
        references,
        reviews_app_client_id=None
        if missing == "client"
        else references.reviews_app_client_id,
        reviews_app_pem=None if missing == "pem" else references.reviews_app_pem,
    )
    with pytest.raises(Invalid) as caught:
        credentials.installation_token(references, "example-org/example-repo")
    assert str(caught.value) == "reviews App credentials are not configured"


@pytest.mark.parametrize(
    ("document", "kind"),
    [
        (["never-log"], "array"),
        (87654321, "number"),
        ("never-log", "string"),
        (None, "null"),
        (True, "boolean"),
    ],
)
def test_auth_file_refusal_names_json_type_without_value(
    tmp_path: Path, document: object, kind: str
) -> None:
    source = runfiles.private_dir(tmp_path / "host") / "auth.json"
    target = runfiles.private_dir(tmp_path / "child") / "auth.json"
    source.write_text("{}")
    target.write_text(json.dumps(document))
    assert credentials.auth_write_back(source, target, "{}") == (
        f"codex auth write-back skipped: auth: found {kind}; expected object\n"
    )
    assert source.read_text() == "{}"

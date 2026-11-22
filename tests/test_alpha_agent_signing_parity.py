"""The signing key parity check compares fingerprints, never key text."""

from __future__ import annotations

import io
import json
import shutil
import subprocess
import urllib.error
from typing import TYPE_CHECKING, Self

import pytest
from agent_example import LINUX, MACOS, settings

from tbhbdev.alpha import agent
from tbhbdev.alpha.agent import driver, signing_parity, transport
from tbhbdev.alpha.agent import settings as agent_settings
from tbhbdev.alpha.agent.model import Invalid

if TYPE_CHECKING:
    from pathlib import Path

    from tbhbdev.alpha.agent.model import Host, Settings

pytestmark = pytest.mark.skipif(
    shutil.which("ssh-keygen") is None, reason="needs ssh-keygen"
)


@pytest.fixture
def key(tmp_path: Path) -> tuple[Path, str]:
    private = tmp_path / "signing"
    subprocess.run(
        ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(private)],
        check=True,
    )
    listed = subprocess.run(
        ["ssh-keygen", "-l", "-f", str(private)],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return private, listed.split()[1]


def signed(path: Path) -> Settings:
    return settings(reference=str(path))


def test_fingerprint_matches_ssh_keygen(key: tuple[Path, str]) -> None:
    private, expected = key
    public = private.with_suffix(".pub").read_text()
    assert signing_parity.fingerprint(public) == expected


def test_fingerprint_refuses_a_line_without_a_key() -> None:
    with pytest.raises(ValueError, match="not an OpenSSH public key"):
        signing_parity.fingerprint("ssh-ed25519")


def test_host_fingerprint_reads_the_configured_key(key: tuple[Path, str]) -> None:
    private, expected = key
    assert signing_parity.host_fingerprint(signed(private), "agent") == expected


def test_host_fingerprint_refuses_an_unknown_account(key: tuple[Path, str]) -> None:
    with pytest.raises(Invalid, match="no signing key"):
        signing_parity.host_fingerprint(signed(key[0]), "nobody")


def test_host_main_prints_only_the_fingerprint(
    key: tuple[Path, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    private, expected = key
    monkeypatch.setattr(agent_settings, "load", lambda: signed(private))
    assert signing_parity.host_main(["agent"], "p") == 0
    assert capsys.readouterr().out == f"{expected}\n"


def test_host_main_failure_names_no_key_text(
    key: tuple[Path, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    private, _ = key
    private.chmod(0o644)
    monkeypatch.setattr(agent_settings, "load", lambda: signed(private))
    assert signing_parity.host_main(["agent"], "p") == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "BEGIN" not in captured.err


@pytest.mark.parametrize(("argv", "code"), [([], 2), (["-h"], 0)])
def test_host_main_arguments(argv: list[str], code: int) -> None:
    assert signing_parity.host_main(argv, "p") == code


class Reply(io.BytesIO):
    def __enter__(self) -> Self:
        return self


def test_registered_lists_fingerprints(
    key: tuple[Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    private, expected = key
    body = json.dumps([{"key": private.with_suffix(".pub").read_text().strip()}])

    def urlopen(_request: object, *, timeout: float) -> Reply:
        assert timeout > 0
        return Reply(body.encode())

    monkeypatch.setattr(signing_parity.urllib.request, "urlopen", urlopen)
    assert signing_parity.registered() == {expected}


def test_remote_fingerprint_runs_the_host_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scripts: list[str] = []

    def shell(_settings: Settings, host: Host, script: str) -> str:
        scripts.append(script)
        return f"SHA256:{host.name}\n"

    monkeypatch.setattr(transport, "shell", shell)
    found = signing_parity.remote_fingerprint(settings(), LINUX)
    assert found == "SHA256:example-linux"
    assert scripts[0].endswith("alpha agent signing-fingerprint agent\n")


def fake_hosts(monkeypatch: pytest.MonkeyPatch) -> None:
    def shell(_settings: Settings, host: Host, _script: str) -> str:
        if host is MACOS:
            message = "example-macos: transport failed"
            raise transport.TransportError(message)
        return "SHA256:linux\n"

    monkeypatch.setattr(transport, "shell", shell)


def test_check_marks_missing_and_unreachable_hosts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_hosts(monkeypatch)
    lines, missing = signing_parity.check(settings(), {"SHA256:other"})
    assert lines == [
        "example-linux: SHA256:linux MISSING",
        "example-macos: unreadable (example-macos: transport failed)",
    ]
    assert missing == ["example-linux", "example-macos"]


def test_check_passes_a_listed_host(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_hosts(monkeypatch)
    lines, missing = signing_parity.check(settings(), {"SHA256:linux"})
    assert lines[0] == "example-linux: SHA256:linux listed"
    assert missing == ["example-macos"]


def test_main_exits_one_when_the_list_is_unreadable(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def registered() -> set[str]:
        message = "offline"
        raise urllib.error.URLError(message)

    monkeypatch.setattr(signing_parity, "registered", registered)
    assert signing_parity.main(settings()) == 1
    assert "cannot read the signing key list" in capsys.readouterr().err


def test_main_exit_status_follows_coverage(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(signing_parity, "registered", lambda: {"SHA256:linux"})
    monkeypatch.setattr(
        signing_parity,
        "check",
        lambda _settings, _known: (["x: SHA256:linux listed"], []),
    )
    assert signing_parity.main(settings()) == 0
    monkeypatch.setattr(
        signing_parity, "check", lambda _settings, _known: (["y"], ["host-y"])
    )
    assert signing_parity.main(settings()) == 1
    assert "not covered: host-y" in capsys.readouterr().err


def test_the_verb_and_the_host_command_are_wired(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(agent_settings, "load", settings)
    monkeypatch.setattr(driver.signing_parity, "main", lambda _settings: 7)
    assert agent.main(["signing-parity"], "p") == 7
    monkeypatch.setattr(signing_parity, "host_main", lambda _argv, _prog: 9)
    assert agent.main(["signing-fingerprint", "agent"], "p") == 9

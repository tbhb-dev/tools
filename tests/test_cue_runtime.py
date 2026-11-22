import hashlib
import io
import tarfile
from typing import cast
lazy from pathlib import Path

import httpx
import pytest

from tbhbdev import cue_runtime as cr

SCRIPT = b"#!/bin/sh\necho 'cue version v9.9.9'\n"
URL = f"{cr.RELEASES}v9.9.9/cue_v9.9.9_linux_amd64.tar.gz"


def archive(member: str = "cue", content: bytes = SCRIPT) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        info = tarfile.TarInfo(member)
        info.size = len(content)
        info.mode = 0o755
        tar.addfile(info, io.BytesIO(content))
    return buffer.getvalue()


def pin_for(data: bytes, url: str = URL, version: str = "9.9.9") -> cr.Pin:
    sha = hashlib.sha256(data).hexdigest()
    return {
        "version": version,
        "platforms": {"linux_amd64": {"url": url, "sha256": sha}},
    }


@pytest.fixture(autouse=True)
def data_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    return tmp_path


def test_shipped_pin_covers_the_supported_platforms() -> None:
    pin = cr.load_pin()
    platforms = cast("dict[str, dict[str, str]]", pin["platforms"])
    assert {"darwin_arm64", "linux_amd64", "linux_arm64"} <= set(platforms)
    assert all(len(entry["sha256"]) == 64 for entry in platforms.values())


@pytest.mark.parametrize(
    ("system", "machine", "key"),
    [
        ("Darwin", "arm64", "darwin_arm64"),
        ("Linux", "x86_64", "linux_amd64"),
        ("Linux", "aarch64", "linux_arm64"),
    ],
)
def test_platform_key(system: str, machine: str, key: str) -> None:
    assert cr.platform_key(system, machine) == key


def test_platform_key_refuses_unknown_platform() -> None:
    with pytest.raises(cr.CueError, match="no pinned cue for Windows"):
        cr.platform_key("Windows", "AMD64")


def test_resolve_refuses_when_not_installed() -> None:
    with pytest.raises(cr.CueError, match="run tbhbdev cue install"):
        cr.resolve(pin_for(archive()))


def test_install_verifies_then_installs_and_resolves(data_home: Path) -> None:
    data = archive()
    pin = pin_for(data)
    path = cr.install(pin, key="linux_amd64", fetch=lambda _: data)
    assert path == data_home / "tbhbdev/cue/9.9.9/cue"
    assert cr.resolve(pin) == path


def test_install_does_not_fetch_when_current() -> None:
    data = archive()
    pin = pin_for(data)
    cr.install(pin, key="linux_amd64", fetch=lambda _: data)

    def refuse(_: str) -> bytes:
        raise AssertionError

    assert cr.install(pin, key="linux_amd64", fetch=refuse)


def test_install_refuses_a_digest_mismatch_and_writes_nothing(data_home: Path) -> None:
    pin = pin_for(archive())
    with pytest.raises(cr.CueError, match="does not match the pin"):
        cr.install(pin, key="linux_amd64", fetch=lambda _: archive(content=b"other"))
    assert not (data_home / "tbhbdev").exists()


def test_install_refuses_an_archive_without_cue() -> None:
    data = archive(member="readme")
    with pytest.raises(cr.CueError, match="no cue binary"):
        cr.install(pin_for(data), key="linux_amd64", fetch=lambda _: data)


def test_install_refuses_a_url_outside_the_release_host() -> None:
    data = archive()
    pin = pin_for(data, url="https://example.com/cue.tar.gz")
    with pytest.raises(cr.CueError, match="outside"):
        cr.install(pin, key="linux_amd64", fetch=lambda _: data)


def test_resolve_refuses_a_wrong_version() -> None:
    data = archive(content=b"#!/bin/sh\necho 'cue version v1.0.0'\n")
    pin = pin_for(data)
    with pytest.raises(cr.CueError, match=r"not v9\.9\.9"):
        cr.install(pin, key="linux_amd64", fetch=lambda _: data)


def test_main_usage_error(capsys: pytest.CaptureFixture[str]) -> None:
    assert cr.main([], "tbhbdev cue") == 2
    assert "usage" in capsys.readouterr().err


def test_install_refuses_a_platform_without_an_archive() -> None:
    with pytest.raises(cr.CueError, match="no pinned cue archive"):
        cr.install(pin_for(archive()), key="darwin_arm64", fetch=lambda _: b"")


def test_fetch_returns_the_response_body(monkeypatch: pytest.MonkeyPatch) -> None:
    def get(url: str, **_: object) -> httpx.Response:
        return httpx.Response(200, content=b"body", request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "get", get)
    assert cr._fetch(URL) == b"body"  # noqa: SLF001 -- The download seam.


def test_main_path_and_install(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    data = archive()
    pin = pin_for(data)
    monkeypatch.setattr(cr, "load_pin", lambda: pin)
    monkeypatch.setattr(cr, "platform_key", lambda *_: "linux_amd64")

    def get(url: str, **_: object) -> httpx.Response:
        return httpx.Response(200, content=data, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "get", get)
    assert cr.main(["path"], "tbhbdev cue") == 1
    assert "run tbhbdev cue install" in capsys.readouterr().err
    assert cr.main(["install"], "tbhbdev cue") == 0
    assert capsys.readouterr().out.strip().endswith("9.9.9/cue")
    assert cr.main(["path"], "tbhbdev cue") == 0

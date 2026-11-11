"""Standalone releases are verified outside the denied account state."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path

import pytest

from tbhbdev.alpha.agent import codex_install
from tbhbdev.alpha.agent.model import Invalid


def release(root: Path) -> Path:
    source = root / ".codex/packages/standalone/releases/example-version"
    (source / "bin").mkdir(parents=True)
    (source / "bin/codex").write_bytes(b"binary")
    (source / "bin/codex").chmod(0o751)
    (source / "codex-resources").mkdir(mode=0o700)
    (source / "codex-resources/helper").write_bytes(b"helper")
    (source / "codex-path").mkdir()
    (source / "codex-path/helper").symlink_to("../codex-resources/helper")
    (source / "codex-package.json").write_text(json.dumps({"entrypoint": "bin/codex"}))
    return source


@pytest.mark.parametrize("xdg", [None, "", "relative", "absolute"])
def test_standalone_copy_is_complete_and_reused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, xdg: str | None
) -> None:
    source = release(tmp_path)
    if xdg is None:
        monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    else:
        monkeypatch.setenv(
            "XDG_DATA_HOME", str(tmp_path / "data") if xdg == "absolute" else xdg
        )
    data = tmp_path / ("data" if xdg == "absolute" else ".local/share")
    expected = data / "tbhbdev/codex/example-version"
    binary, roots = codex_install.prepare(str(source / "bin/codex"), tmp_path)
    assert binary == str(expected / "bin/codex")
    assert roots == (str(expected),)
    assert (expected / "bin/codex").read_bytes() == b"binary"
    assert stat.S_IMODE((expected / "bin/codex").stat().st_mode) == 0o751
    assert stat.S_IMODE((expected / "codex-resources").stat().st_mode) == 0o755
    assert (expected / "codex-path/helper").readlink() == Path(
        "../codex-resources/helper"
    )
    before = (expected / "bin/codex").stat().st_ino
    assert codex_install.prepare(str(source / "bin/codex"), tmp_path) == (binary, roots)
    assert (expected / "bin/codex").stat().st_ino == before
    assert not list(expected.parent.glob("*.old-*"))


@pytest.mark.parametrize(
    "tamper",
    ["bytes", "mode", "extra", "missing", "directory", "root", "link", "target"],
)
def test_tampered_copy_is_moved_aside(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tamper: str
) -> None:
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    source = release(tmp_path)
    binary, roots = codex_install.prepare(str(source / "bin/codex"), tmp_path)
    destination = Path(roots[0])
    if tamper == "bytes":
        Path(binary).write_bytes(b"tampered")
    elif tamper == "mode":
        Path(binary).chmod(0o777)
    elif tamper == "extra":
        (destination / "extra").write_text("extra")
    elif tamper == "missing":
        Path(binary).rename(tmp_path / "saved")
    elif tamper == "root":
        destination.chmod(0o700)
    elif tamper == "target":
        link = destination / "codex-path/helper"
        link.rename(tmp_path / "saved-link")
        link.symlink_to("../bin/codex")
    elif tamper == "directory":
        (destination / "bin").chmod(0o700)
    else:
        (destination / "extra").symlink_to(tmp_path / "outside")
    assert codex_install.prepare(str(source / "bin/codex"), tmp_path) == (binary, roots)
    assert Path(binary).read_bytes() == b"binary"
    assert stat.S_IMODE(Path(binary).stat().st_mode) == 0o751
    assert len(list(destination.parent.glob("*.old-*"))) == 1


@pytest.mark.parametrize("existing", [False, True])
def test_copy_that_fails_verification_refuses_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, existing: bool
) -> None:
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    source = release(tmp_path)
    if existing:
        binary, _ = codex_install.prepare(str(source / "bin/codex"), tmp_path)
        Path(binary).write_bytes(b"tampered")
    original = codex_install.copy_release

    def damaged(source: Path, target: Path) -> None:
        original(source, target)
        if source.name == "example-version":
            assert target.parent == tmp_path / ".local/share/tbhbdev/codex"
            assert target.name != source.name
            (target / "bin/codex").write_text("damaged")

    monkeypatch.setattr(codex_install, "copy_release", damaged)
    with pytest.raises(Invalid, match=r"Codex copy.*example-version"):
        codex_install.prepare(str(source / "bin/codex"), tmp_path)


@pytest.mark.parametrize("entrypoint", ["../outside", "/outside", "missing", 1])
def test_unsafe_entrypoint_refuses_launch(
    tmp_path: Path, entrypoint: str | int
) -> None:
    source = release(tmp_path)
    (source / "codex-package.json").write_text(json.dumps({"entrypoint": entrypoint}))
    with pytest.raises(Invalid, match="entrypoint"):
        codex_install.prepare(str(source / "bin/codex"), tmp_path)


@pytest.mark.parametrize("manifest", [False, True])
def test_non_standalone_refuses_launch(tmp_path: Path, manifest: bool) -> None:
    source = release(tmp_path)
    executable = source / "bin/codex" if not manifest else tmp_path / "codex"
    if manifest:
        executable.write_bytes(b"other")
        (tmp_path / "codex-package.json").write_text("{}")
    else:
        (source / "codex-package.json").rename(tmp_path / "saved.json")
    with pytest.raises(Invalid) as caught:
        codex_install.prepare(str(executable), tmp_path)
    assert str(caught.value) == (
        f"standalone Codex install required: {executable.resolve()}"
    )


def test_non_standalone_refusal_names_the_resolved_binary(tmp_path: Path) -> None:
    executable = tmp_path / "binary"
    executable.write_bytes(b"fixture")
    alias = tmp_path / "codex"
    alias.symlink_to(executable)
    with pytest.raises(Invalid) as caught:
        codex_install.prepare(str(alias), tmp_path)
    assert str(caught.value) == f"standalone Codex install required: {executable}"


@pytest.mark.parametrize("target", ["outside", "absolute", "relative"])
def test_release_links_stay_within_the_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, target: str
) -> None:
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    source = release(tmp_path)
    link = source / "linked"
    link.symlink_to(
        source / "bin/codex"
        if target == "absolute"
        else "../outside"
        if target == "outside"
        else "bin/codex"
    )
    if target == "outside":
        with pytest.raises(Invalid, match=r"symlink.*linked"):
            codex_install.prepare(str(source / "bin/codex"), tmp_path)
    else:
        _, roots = codex_install.prepare(str(source / "bin/codex"), tmp_path)
        assert (Path(roots[0]) / "linked").resolve() == Path(roots[0]) / "bin/codex"


def test_special_release_file_is_refused(tmp_path: Path) -> None:
    source = release(tmp_path)
    os.mkfifo(source / "pipe")
    with pytest.raises(Invalid, match=r"unsupported Codex release file.*pipe"):
        codex_install.prepare(str(source / "bin/codex"), tmp_path)


@pytest.mark.parametrize("text", ["{", "{}", "[]"])
def test_invalid_package_manifest_is_refused(tmp_path: Path, text: str) -> None:
    source = release(tmp_path)
    (source / "codex-package.json").write_text(text)
    with pytest.raises(Invalid, match="entrypoint"):
        codex_install.prepare(str(source / "bin/codex"), tmp_path)


def test_symlinked_copy_is_replaced_without_touching_its_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    source = release(tmp_path)
    parent = tmp_path / ".local/share/tbhbdev/codex"
    parent.mkdir(parents=True)
    (parent / source.name).symlink_to(source, target_is_directory=True)
    binary, roots = codex_install.prepare(str(source / "bin/codex"), tmp_path)
    assert not Path(roots[0]).is_symlink()
    assert Path(binary).read_bytes() == b"binary"
    assert source.is_dir()
    assert next(parent.glob("*.old-*")).is_symlink()


def test_inventory_records_file_digests_modes_and_link_targets(tmp_path: Path) -> None:
    source = release(tmp_path)
    expected = {
        ".": (stat.S_IFDIR, ""),
        "bin": (stat.S_IFDIR, ""),
        "bin/codex": (0o751, hashlib.sha256(b"binary").hexdigest()),
        "codex-resources": (stat.S_IFDIR, ""),
        "codex-resources/helper": (
            stat.S_IMODE((source / "codex-resources/helper").stat().st_mode),
            hashlib.sha256(b"helper").hexdigest(),
        ),
        "codex-path": (stat.S_IFDIR, ""),
        "codex-path/helper": (stat.S_IFLNK, "codex-resources/helper"),
        "codex-package.json": (
            stat.S_IMODE((source / "codex-package.json").stat().st_mode),
            hashlib.sha256((source / "codex-package.json").read_bytes()).hexdigest(),
        ),
    }
    assert codex_install.inventory(source) == expected


@pytest.fixture(autouse=True)
def portable_manifest(monkeypatch: pytest.MonkeyPatch) -> None:
    original_read = Path.read_text
    original_file = Path.is_file

    def check(path: Path) -> None:
        if path.name.lower() == "codex-package.json":
            assert path.name == "codex-package.json"

    def read(
        path: Path,
        encoding: str | None = None,
        errors: str | None = None,
        newline: str | None = None,
    ) -> str:
        check(path)
        return original_read(path, encoding=encoding, errors=errors, newline=newline)

    def is_file(path: Path) -> bool:
        check(path)
        return original_file(path)

    monkeypatch.setattr(Path, "read_text", read)
    monkeypatch.setattr(Path, "is_file", is_file)


def test_manifest_selects_an_entrypoint_other_than_the_found_binary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    source = release(tmp_path)
    selected = source / "launch"
    selected.write_bytes(b"selected")
    selected.chmod(0o755)
    (source / "codex-package.json").write_text('{"entrypoint":"launch"}')
    binary, roots = codex_install.prepare(str(source / "bin/codex"), tmp_path)
    assert binary == str(Path(roots[0]) / "launch")
    assert Path(binary).read_bytes() == b"selected"


def test_entrypoint_symlink_cannot_escape_the_release(tmp_path: Path) -> None:
    source = release(tmp_path)
    outside = tmp_path / "outside"
    outside.write_bytes(b"outside")
    (source / "launch").symlink_to(outside)
    (source / "codex-package.json").write_text('{"entrypoint":"launch"}')
    with pytest.raises(Invalid, match="entrypoint"):
        codex_install.prepare(str(source / "bin/codex"), tmp_path)

# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

"""Tests for the pinned ccstatusline install, with no network, home or bun."""

import json
import os
import shlex
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from tbhbdev.alpha import install

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

OLD = install.OLD_STATUSLINES[0]
BARE = install.OLD_STATUSLINES[1]
PACKAGE_FILE = "console.log(1)\n"
FOREIGN: dict[str, object] = {"model": "opus", "env": {"A": "b"}, "hooks": {"Stop": []}}

pytestmark = pytest.mark.usefixtures("isolated_home")  # noqa: V107


@dataclass
class Tools:
    """A fake bun runner and a fake PATH lookup."""

    root: Path
    found: dict[str, str] = field(
        default_factory=lambda: {"bun": "/x/bun", "node": "/x/node bin/node"}
    )
    calls: list[tuple[list[str], Mapping[str, object]]] = field(default_factory=list)
    fail: Exception | None = None
    fail_after_write: Exception | None = None
    skip_script: bool = False
    during: Callable[[], None] | None = None
    cwd: Path | None = None

    def which(self, name: str) -> str | None:
        return self.found.get(name)

    def run(self, argv: list[str], **kwargs: object) -> None:
        self.calls.append((argv, kwargs))
        self.cwd = Path(str(kwargs["cwd"]))
        if self.during:
            self.during()
        if self.fail:
            raise self.fail
        if not self.skip_script:
            script = install.statusline_script(Path(str(kwargs["cwd"])))
            script.parent.mkdir(parents=True)
            script.write_text(PACKAGE_FILE)
        if self.fail_after_write:
            raise self.fail_after_write


@dataclass
class Setup:
    env: dict[str, str]
    path: Path
    tools: Tools

    def write(self, config: object) -> str:
        text = json.dumps(config, indent=2) + "\n"
        self.path.parent.mkdir(exist_ok=True)
        self.path.write_text(text)
        return text

    def go(self, *, dry_run: bool = False) -> int:
        return install.install_statusline(
            self.env, dry_run=dry_run, run=self.tools.run, which=self.tools.which
        )

    def backups(self) -> list[Path]:
        return sorted(self.path.parent.glob("settings.json.tbhbdev-backup.*"))


def preinstall(root: Path) -> Path:
    """Make ROOT a complete install, as a clean bun run leaves it."""
    script = install.statusline_script(root)
    script.parent.mkdir(parents=True)
    script.write_text(PACKAGE_FILE)
    (root / install.STATUSLINE_MARKER).write_text(install.STATUSLINE_VERSION)
    return script


@pytest.fixture
def setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Setup:
    reference = tmp_path / "reference"
    install.statusline_script(reference).parent.mkdir(parents=True)
    install.statusline_script(reference).write_text(PACKAGE_FILE)
    monkeypatch.setattr(
        install,
        "STATUSLINE_DIGEST",
        install.package_digest(install.statusline_package(reference)),
    )
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    config = tmp_path / "claude"
    root = install.statusline_root()
    return Setup(
        {"CLAUDE_CONFIG_DIR": str(config)}, config / "settings.json", Tools(root)
    )


def command(setup: Setup) -> str:
    return str(json.loads(setup.path.read_text())["statusLine"]["command"])


def wanted(setup: Setup) -> str:
    script = install.statusline_script(setup.tools.root)
    return install.statusline_command("/x/node bin/node", script)


@pytest.mark.parametrize("old", install.OLD_STATUSLINES)
def test_pins_the_known_command_and_keeps_every_other_key(
    setup: Setup, old: str, capsys: pytest.CaptureFixture[str]
) -> None:
    line = {"type": "command", "command": old, "padding": 0}
    before = setup.write({**FOREIGN, "statusLine": line})
    assert setup.go() == 0
    script = install.statusline_script(setup.tools.root)
    assert setup.tools.calls == [
        (
            ["/x/bun", "add", "--exact", "--ignore-scripts", "ccstatusline@2.2.33"],
            {
                "cwd": setup.tools.cwd,
                "check": True,
                "capture_output": True,
                "timeout": install.STATUSLINE_TIMEOUT,
            },
        )
    ]
    assert setup.tools.cwd is not None
    assert setup.tools.cwd.parent == setup.tools.root.parent
    assert setup.tools.cwd.name.startswith(".2.2.33.staging-")
    assert not setup.tools.cwd.exists()
    assert setup.tools.root.parts[-3:] == ("tbhbdev", "ccstatusline", "2.2.33")
    after = json.loads(setup.path.read_text())
    assert {k: v for k, v in after.items() if k != "statusLine"} == FOREIGN
    assert after["statusLine"] == {**line, "command": wanted(setup)}
    assert wanted(setup).endswith(f"'/x/node bin/node' {script}")
    expected = before.replace(json.dumps(old), json.dumps(wanted(setup)))
    assert setup.path.read_text() == expected
    (backup,) = setup.backups()
    assert backup.read_text() == before
    assert capsys.readouterr().out == (
        f"claude: pinned ccstatusline 2.2.33 in {setup.path}; the old one is {backup}\n"
    )


def test_an_edit_made_during_the_install_survives(setup: Setup) -> None:
    line = {"type": "command", "command": OLD}
    setup.write({**FOREIGN, "statusLine": line})

    def edit() -> None:
        setup.write({**FOREIGN, "model": "sonnet", "statusLine": line})

    setup.tools.during = edit
    assert setup.go() == 0
    after = json.loads(setup.path.read_text())
    assert after["model"] == "sonnet"
    assert after["statusLine"]["command"] == wanted(setup)


def test_a_status_line_changed_during_the_install_is_left_alone(
    setup: Setup, capsys: pytest.CaptureFixture[str]
) -> None:
    setup.write({"statusLine": {"type": "command", "command": OLD}})
    edited = {"statusLine": {"type": "command", "command": "mine"}}

    def edit() -> None:
        setup.write(edited)

    setup.tools.during = edit
    assert setup.go() == 0
    assert json.loads(setup.path.read_text()) == edited
    assert not setup.backups()
    assert capsys.readouterr().err == (
        f"claude: statusLine in {setup.path} changed while ccstatusline installed;"
        " left as it is\n"
    )


def test_a_moved_node_rewrites_our_own_command(setup: Setup) -> None:
    script = preinstall(setup.tools.root)
    setup.write(
        {
            "statusLine": {
                "type": "command",
                "command": install.statusline_command("/old/node", script),
            }
        }
    )
    assert setup.go() == 0
    assert not setup.tools.calls
    assert command(setup) == wanted(setup)
    assert len(setup.backups()) == 1


@pytest.mark.parametrize("suffix", ["", " --extra"])
def test_a_command_that_only_resembles_ours_stays(setup: Setup, suffix: str) -> None:
    script = preinstall(setup.tools.root)
    other = install.statusline_command("/a b/node x", script).replace(
        "'/a b/node x'", "node two"
    )
    before = setup.write({"statusLine": {"type": "command", "command": other + suffix}})
    assert setup.go() == 0
    assert setup.path.read_text() == before


def test_ours_is_the_installers_own_command_for_any_node_and_version(
    setup: Setup,
) -> None:
    base = setup.tools.root.parent
    for version in ("2.2.33", "2.3.0"):
        script = install.statusline_script(base / version)
        assert install.is_ours(install.statusline_command("/a b/node", script))
    script = install.statusline_script(setup.tools.root)
    assert install.is_ours(install.statusline_command("/x/bin/node", script))


@pytest.mark.parametrize(
    "node", ["node", "evil-binary", "$MY_STATUS_NODE", "/x/bin/evil", "~/node"]
)
def test_a_node_slot_other_than_an_absolute_node_is_not_ours(
    setup: Setup, node: str
) -> None:
    script = install.statusline_script(setup.tools.root)
    assert not install.is_ours(
        f"{install.COLUMNS_PREFIX} {node} {shlex.quote(str(script))}"
    )


def test_a_command_not_in_the_installers_quoting_or_place_is_not_ours(
    setup: Setup,
) -> None:
    script = install.statusline_script(setup.tools.root)
    head = f"{install.COLUMNS_PREFIX} "
    assert not install.is_ours(f"{head}/x/node {script}   ")
    assert not install.is_ours(f'{head}"/x/node" {shlex.quote(str(script))}')
    assert not install.is_ours(f"{head}/x/node {shlex.quote(str(script))} --extra")
    assert not install.is_ours(f"{head}/x/node /elsewhere/dist/ccstatusline.js")
    other = Path("/elsewhere/2.2.33/node_modules/ccstatusline/dist/ccstatusline.js")
    assert not install.is_ours(install.statusline_command("/x/node", other))
    assert not install.is_ours(f"x{head}/x/node {shlex.quote(str(script))}")


def test_a_version_bump_replaces_the_command_written_for_the_old_version(
    setup: Setup,
) -> None:
    old = install.statusline_script(setup.tools.root.parent / "2.2.0")
    setup.write(
        {
            "statusLine": {
                "type": "command",
                "command": install.statusline_command("/x/node bin/node", old),
            }
        }
    )
    assert setup.go() == 0
    assert command(setup) == wanted(setup)
    assert len(setup.backups()) == 1


def test_a_command_with_an_unbalanced_quote_stays(setup: Setup) -> None:
    script = preinstall(setup.tools.root)
    other = f"{install.COLUMNS_PREFIX} 'node {shlex.quote(str(script))}"
    before = setup.write({"statusLine": {"type": "command", "command": other}})
    assert setup.go() == 0
    assert setup.path.read_text() == before


def test_the_settings_and_backup_keep_the_file_mode(setup: Setup) -> None:
    setup.write({"statusLine": {"type": "command", "command": OLD}})
    setup.path.chmod(0o600)
    assert setup.go() == 0
    (backup,) = setup.backups()
    assert setup.path.stat().st_mode & 0o777 == 0o600
    assert backup.stat().st_mode & 0o777 == 0o600


def test_an_existing_install_is_reused_without_bun(setup: Setup) -> None:
    preinstall(setup.tools.root)
    del setup.tools.found["bun"]
    setup.write({"statusLine": {"type": "command", "command": OLD}})
    assert setup.go() == 0
    assert not setup.tools.calls
    assert command(setup) == wanted(setup)


def test_a_rerun_changes_nothing(
    setup: Setup, capsys: pytest.CaptureFixture[str]
) -> None:
    setup.write({"statusLine": {"type": "command", "command": OLD}})
    assert setup.go() == 0
    capsys.readouterr()
    text = setup.path.read_text()
    assert setup.go() == 0
    assert (
        capsys.readouterr().out == f"claude: statusLine in {setup.path} is up to date\n"
    )
    assert len(setup.tools.calls) == 1
    assert setup.path.read_text() == text
    assert len(setup.backups()) == 1


@pytest.mark.parametrize(
    "config",
    [
        {"model": "opus"},
        {"statusLine": "text"},
        {"statusLine": {"type": "command"}},
        {"statusLine": {"type": "command", "command": 5}},
        {"statusLine": {"type": "command", "command": "~/bin/my-line"}},
        {"statusLine": {"type": "command", "command": f"{BARE} --json"}},
        {"statusLine": {"type": "command", "command": "bunx ccstatusline@2.2.0"}},
    ],
)
def test_a_custom_or_missing_statusline_stays(
    setup: Setup, config: object, capsys: pytest.CaptureFixture[str]
) -> None:
    before = setup.write(config)
    assert setup.go() == 0
    assert setup.path.read_text() == before
    assert not setup.tools.calls
    assert not setup.backups()
    assert capsys.readouterr() == ("", "")


def test_no_settings_file_is_left_missing(setup: Setup) -> None:
    assert setup.go() == 0
    assert not setup.path.exists()
    assert not setup.tools.calls


@pytest.mark.parametrize("missing", ["node", "bun"])
def test_missing_bun_or_node_keeps_the_old_command(
    setup: Setup, capsys: pytest.CaptureFixture[str], missing: str
) -> None:
    del setup.tools.found[missing]
    before = setup.write({"statusLine": {"type": "command", "command": OLD}})
    assert setup.go() == 0
    assert setup.path.read_text() == before
    assert not setup.tools.calls
    assert capsys.readouterr() == (
        "",
        f"claude: {missing} not found; left the statusLine command in {setup.path}\n",
    )


def test_a_pinned_command_without_its_install_reinstalls(setup: Setup) -> None:
    setup.write({"statusLine": {"type": "command", "command": wanted(setup)}})
    assert setup.go() == 0
    assert len(setup.tools.calls) == 1
    assert install.statusline_script(setup.tools.root).is_file()
    assert command(setup) == wanted(setup)


def test_a_pinned_command_without_its_install_or_bun_says_so(
    setup: Setup, capsys: pytest.CaptureFixture[str]
) -> None:
    del setup.tools.found["bun"]
    setup.write({"statusLine": {"type": "command", "command": wanted(setup)}})
    assert setup.go() == 0
    assert "bun not found" in capsys.readouterr().err
    assert not setup.tools.calls


def test_a_pinned_command_without_node_is_not_ours_to_judge(setup: Setup) -> None:
    other = "/y/node /y/ccstatusline.js"
    before = setup.write({"statusLine": {"type": "command", "command": other}})
    del setup.tools.found["node"]
    assert setup.go() == 0
    assert setup.path.read_text() == before


def test_our_command_without_node_says_node_is_missing(
    setup: Setup, capsys: pytest.CaptureFixture[str]
) -> None:
    script = preinstall(setup.tools.root)
    other = install.statusline_command("/old/node", script)
    before = setup.write({"statusLine": {"type": "command", "command": other}})
    del setup.tools.found["node"]
    assert setup.go() == 0
    assert setup.path.read_text() == before
    assert "node not found" in capsys.readouterr().err


def test_a_reinstall_into_an_existing_root_succeeds(setup: Setup) -> None:
    setup.tools.root.mkdir(parents=True)
    (setup.tools.root / "stale").write_text("")
    setup.write({"statusLine": {"type": "command", "command": OLD}})
    assert setup.go() == 0
    assert command(setup) == wanted(setup)
    assert not (setup.tools.root / "stale").exists()


@pytest.mark.parametrize(
    "failure",
    [subprocess.TimeoutExpired("bun", 1), subprocess.CalledProcessError(1, "bun")],
)
def test_a_bun_that_wrote_the_script_then_failed_leaves_no_install(
    setup: Setup, failure: Exception, capsys: pytest.CaptureFixture[str]
) -> None:
    before = setup.write({"statusLine": {"type": "command", "command": OLD}})
    setup.tools.fail_after_write = failure
    assert setup.go() == 1
    assert setup.path.read_text() == before
    assert not setup.tools.root.exists()
    assert not statusline_installed(setup)
    assert "cannot pin ccstatusline" in capsys.readouterr().err
    setup.tools.fail_after_write = None
    assert setup.go() == 0
    assert len(setup.tools.calls) == 2
    assert command(setup) == wanted(setup)
    assert install.statusline_installed(setup.tools.root)


def statusline_installed(setup: Setup) -> bool:
    return install.statusline_installed(setup.tools.root)


def test_a_script_without_the_marker_is_not_a_complete_install(
    setup: Setup,
) -> None:
    script = install.statusline_script(setup.tools.root)
    script.parent.mkdir(parents=True)
    script.write_text("")
    setup.write({"statusLine": {"type": "command", "command": wanted(setup)}})
    assert not statusline_installed(setup)
    assert setup.go() == 0
    assert len(setup.tools.calls) == 1
    assert statusline_installed(setup)


def test_a_symlinked_script_or_root_is_not_trusted(
    setup: Setup, tmp_path: Path
) -> None:
    real = tmp_path / "real"
    preinstall(real)
    setup.tools.root.parent.mkdir(parents=True)
    setup.tools.root.symlink_to(real)
    assert not statusline_installed(setup)
    setup.write({"statusLine": {"type": "command", "command": wanted(setup)}})
    assert setup.go() == 0
    assert len(setup.tools.calls) == 1
    assert not setup.tools.root.is_symlink()
    assert statusline_installed(setup)
    script = install.statusline_script(setup.tools.root)
    target = tmp_path / "evil.js"
    target.write_text("")
    script.unlink()
    script.symlink_to(target)
    assert not statusline_installed(setup)


def test_a_staged_script_that_is_a_symlink_fails(
    setup: Setup, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target = tmp_path / "evil.js"
    target.write_text("")

    def link() -> None:
        assert setup.tools.cwd is not None
        script = install.statusline_script(setup.tools.cwd)
        script.parent.mkdir(parents=True)
        script.symlink_to(target)

    setup.tools.skip_script = True
    setup.tools.during = link
    setup.write({"statusLine": {"type": "command", "command": OLD}})
    assert setup.go() == 1
    assert not setup.tools.root.exists()
    assert "failed its check" in capsys.readouterr().err


def test_dry_run_writes_and_installs_nothing(
    setup: Setup, capsys: pytest.CaptureFixture[str]
) -> None:
    before = setup.write({"statusLine": {"type": "command", "command": OLD}})
    assert setup.go(dry_run=True) == 0
    assert setup.path.read_text() == before
    assert not setup.tools.calls
    assert not setup.tools.root.exists()
    assert capsys.readouterr().out == (
        f"claude: would install ccstatusline 2.2.33 in {setup.tools.root}\n"
    )


@pytest.mark.parametrize(
    "failure",
    [
        subprocess.CalledProcessError(1, "bun"),
        subprocess.TimeoutExpired("bun", 1),
        OSError("no exec"),
    ],
)
def test_a_failed_install_leaves_the_old_command(
    setup: Setup, failure: Exception, capsys: pytest.CaptureFixture[str]
) -> None:
    before = setup.write({"statusLine": {"type": "command", "command": OLD}})
    setup.tools.fail = failure
    assert setup.go() == 1
    assert setup.path.read_text() == before
    assert not setup.backups()
    assert "cannot pin ccstatusline" in capsys.readouterr().err


def test_another_installers_staging_directory_is_left_alone(setup: Setup) -> None:
    other = setup.tools.root.with_name(".2.2.33.staging-other")
    other.mkdir(parents=True)
    (other / "half-written").write_text("")
    setup.write({"statusLine": {"type": "command", "command": OLD}})
    assert setup.go() == 0
    assert (other / "half-written").is_file()
    assert not (setup.tools.root / "half-written").exists()
    assert setup.tools.cwd != other


def test_a_wrong_marker_or_altered_file_is_not_a_complete_install(
    setup: Setup,
) -> None:
    script = preinstall(setup.tools.root)
    assert statusline_installed(setup)
    marker = setup.tools.root / install.STATUSLINE_MARKER
    marker.write_text("2.2.0")
    assert not statusline_installed(setup)
    marker.write_text(install.STATUSLINE_VERSION)
    script.write_text("evil()\n")
    assert not statusline_installed(setup)
    script.write_text(PACKAGE_FILE)
    (script.parent / "extra.js").write_text("evil()\n")
    assert not statusline_installed(setup)


def test_an_altered_install_is_replaced_not_reused(setup: Setup) -> None:
    script = preinstall(setup.tools.root)
    script.write_text("evil()\n")
    setup.write({"statusLine": {"type": "command", "command": OLD}})
    assert setup.go() == 0
    assert len(setup.tools.calls) == 1
    assert script.read_text() == PACKAGE_FILE


@pytest.mark.parametrize("name", [".complete", "node_modules", "ccstatusline", "dist"])
def test_a_symlink_anywhere_in_the_install_is_not_trusted(
    setup: Setup, tmp_path: Path, name: str
) -> None:
    preinstall(setup.tools.root)
    root = setup.tools.root
    entry = {
        ".complete": root / ".complete",
        "node_modules": root / "node_modules",
        "ccstatusline": root / "node_modules" / "ccstatusline",
        "dist": root / "node_modules" / "ccstatusline" / "dist",
    }[name]
    moved = tmp_path / "moved"
    entry.rename(moved)
    entry.symlink_to(moved)
    assert not statusline_installed(setup)


def test_a_socket_or_fifo_in_the_package_is_not_trusted(setup: Setup) -> None:
    script = preinstall(setup.tools.root)
    os.mkfifo(script.parent / "pipe")
    assert not statusline_installed(setup)


def test_a_settings_edit_between_the_read_and_the_replace_aborts(
    setup: Setup, capsys: pytest.CaptureFixture[str]
) -> None:
    setup.write({"statusLine": {"type": "command", "command": OLD}})
    edited = {"model": "sonnet", "statusLine": {"type": "command", "command": OLD}}
    real = install.check_unchanged

    def edit_then_check(target: Path, old: str | None) -> None:
        setup.write(edited)
        real(target, old)

    install.check_unchanged, saved = edit_then_check, real
    try:
        assert setup.go() == 1
    finally:
        install.check_unchanged = saved
    assert json.loads(setup.path.read_text()) == edited
    assert "changed while it was being written" in capsys.readouterr().err
    assert not list(setup.path.parent.glob("*.tmp"))


def test_an_install_that_leaves_no_script_fails(
    setup: Setup, capsys: pytest.CaptureFixture[str]
) -> None:
    before = setup.write({"statusLine": {"type": "command", "command": OLD}})
    setup.tools.skip_script = True
    assert setup.go() == 1
    assert setup.path.read_text() == before
    assert "failed its check" in capsys.readouterr().err


def test_a_written_value_that_reads_back_differently_fails_loudly(
    setup: Setup,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    setup.write({"statusLine": {"type": "command", "command": OLD}})
    real = install.write_config

    def lying(path: Path, old: str | None, new: str) -> Path | None:
        return real(path, old, new.replace("2.2.33", "9.9.9"))

    monkeypatch.setattr(install, "write_config", lying)
    assert setup.go() == 1
    (backup,) = setup.backups()
    assert (
        f"the written file differs; the old one is {backup}" in capsys.readouterr().err
    )


def test_an_unreadable_config_is_reported(
    setup: Setup, capsys: pytest.CaptureFixture[str]
) -> None:
    setup.path.parent.mkdir()
    setup.path.write_text("{")
    assert setup.go() == 1
    assert f"cannot read {setup.path}: it is not valid JSON" in capsys.readouterr().err


def test_a_failed_write_is_reported(
    setup: Setup, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    before = setup.write({"statusLine": {"type": "command", "command": OLD}})

    def refuse(*_a: object) -> None:
        message = "disk full"
        raise install.InstallError(message)

    monkeypatch.setattr(install, "write_config", refuse)
    assert setup.go() == 1
    assert setup.path.read_text() == before
    assert "cannot pin ccstatusline: disk full" in capsys.readouterr().err


def test_the_root_follows_xdg_data_home_then_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "d"))
    assert (
        install.statusline_root()
        == tmp_path / "d" / "tbhbdev" / "ccstatusline" / "2.2.33"
    )
    monkeypatch.delenv("XDG_DATA_HOME")
    monkeypatch.setenv("HOME", str(tmp_path))
    assert install.statusline_root() == (
        tmp_path / ".local" / "share" / "tbhbdev" / "ccstatusline" / "2.2.33"
    )


def test_the_command_quotes_its_paths_and_keeps_the_width_fallback() -> None:
    text = install.statusline_command("/a b/node", Path("/c d/s.js"))
    assert text == f"{install.COLUMNS_PREFIX} '/a b/node' '/c d/s.js'"
    assert BARE == "bunx -y ccstatusline@latest"
    assert (
        install.COLUMNS_PREFIX
        == "COLUMNS=${COLUMNS:-$(tmux display-message -p '#{pane_width}' 2>/dev/null"
        " || echo 80)}"
    )
    assert f"PATH=/opt/homebrew/bin:$PATH {install.COLUMNS_PREFIX} {BARE}" == OLD
    assert install.statusline_script(Path("/r")) == Path(
        "/r/node_modules/ccstatusline/dist/ccstatusline.js"
    )


def test_home_derived_paths_stay_inside_the_temporary_directory(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:

    paths = (
        Path.home(),
        install.config_path(install.CLAUDE, os.environ),
        install.statusline_root(),
    )
    assert all(path.is_relative_to(tmp_path_factory.getbasetemp()) for path in paths)

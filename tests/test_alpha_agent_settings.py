"""Agent settings load from a config file under a temporary home."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from tbhbdev import alpha, cli, userconfig
from tbhbdev.alpha import agent
from tbhbdev.alpha.agent import model, settings

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

EXAMPLE = """\
[agent]
launch_wait_seconds = 120
agy_timeout = "900s"
kill_grace_seconds = 10
max_run_minutes = 120
sockets_dir = "/example/sockets"

[agent.sandbox]
write_roots = [".cache", ".local/share/mise", ".local/state"]
audit_dir = ".local/state/example-audit"
denied_home_paths = [".claude"]

[agent.sandbox.path]
linux = [".local/share/mise/shims", "/usr/local/bin", "/usr/bin", "/bin"]
macos = [".local/share/mise/shims", "/opt/homebrew/bin", "/usr/bin", "/bin"]

[agent.sandbox.agy]
review_read = [".config/example-gh", ".config/mise"]
build_read = [".config/example-tools", ".local/share/mise/installs"]
build_write = [".local/state/mise", ".local/share/mise"]

[agent.codex]
web_search = { build = "live", review = "cached" }

[agent.credentials.agent]
gh = "example-vault/example-gh/token"
docker = "example-vault/example-docker/config"
reviews_app_id = "example-vault/example-app/app_id"
reviews_app_client_id = "example-vault/example-app/client_id"
reviews_app_pem = "example-vault/example-app/private_key"
signing = "example-vault/example-signing/private_key"

[agent.claude]
default_profile = "primary"

[agent.claude.profiles.primary]
token = "example/primary/token"

[agent.claude.profiles.secondary]
token = "example/secondary/token"
expires = "example/secondary/expires"

[[agent.host]]
name = "example-linux"
os = "linux"
shell = "bash"
account = "agent"
unsandboxed = false
accounts = ["agent", "ops"]

[[agent.host]]
name = "example-macos"
os = "macos"
shell = "zsh"
account = "agent"
unsandboxed = true
accounts = ["agent"]
"""


def test_loads_the_example_from_the_home_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "example-home"
    (home / ".config" / "tbhbdev").mkdir(parents=True)
    (home / ".config" / "tbhbdev" / "config.toml").write_text(EXAMPLE)
    monkeypatch.delenv("XDG_CONFIG_HOME")
    monkeypatch.setenv("HOME", str(home))
    loaded = settings.load()
    assert loaded.launch_wait_seconds == 120
    assert loaded.agy_timeout_seconds == 900
    assert loaded.codex == model.Codex(model.WebSearch.LIVE, model.WebSearch.CACHED)
    assert [h.name for h in loaded.hosts] == ["example-linux", "example-macos"]
    assert loaded.hosts[1].unsandboxed
    assert loaded.claude.default_profile == "primary"
    assert (
        loaded.credentials[0].reviews_app_pem == "example-vault/example-app/private_key"
    )
    assert loaded.sandbox.agy.build_read == (
        ".config/example-tools",
        ".local/share/mise/installs",
    )


def test_missing_file_names_the_config(config_file: Path) -> None:
    with pytest.raises(userconfig.ConfigError) as caught:
        settings.load()
    assert str(caught.value) == (
        f"{config_file}: config cannot be read: "
        f"{config_file} doesn't exist; create it and set [agent]"
    )


def test_bad_table_names_the_config(config_file: Path) -> None:
    config_file.write_text('agent = "invalid"\n')
    with pytest.raises(userconfig.ConfigError) as caught:
        settings.load()
    assert str(caught.value) == (
        f"{config_file}: config cannot be read: {config_file}: agent must be a table"
    )


@pytest.mark.parametrize("text", [b"[agent", b"\xff"])
def test_invalid_file_is_a_configuration_error(config_file: Path, text: bytes) -> None:
    config_file.write_bytes(text)
    with pytest.raises(userconfig.ConfigError) as caught:
        settings.load()
    assert str(caught.value).startswith(f"{config_file}: config cannot be read: ")


def test_unreadable_file_is_a_configuration_error(config_file: Path) -> None:
    config_file.mkdir()
    with pytest.raises(userconfig.ConfigError) as caught:
        settings.load()
    assert str(caught.value).startswith(f"{config_file}: config cannot be read: ")


def test_absent_table_is_an_error(config_file: Path) -> None:
    config_file.write_text('[loop]\nhosts = ["x"]\n')
    with pytest.raises(userconfig.ConfigError) as caught:
        settings.load()
    assert str(caught.value) == f"{config_file}: [agent] table is required"


def test_invalid_setting_names_the_file_table_and_key(config_file: Path) -> None:
    config_file.write_text(
        EXAMPLE.replace('audit_dir = ".local/state/example-audit"\n', "")
    )
    with pytest.raises(userconfig.ConfigError) as caught:
        settings.load()
    assert str(caught.value) == f"{config_file}: [agent.sandbox] audit_dir is required"


def test_unknown_key_names_the_full_table_path(config_file: Path) -> None:
    config_file.write_text(
        EXAMPLE.replace('shell = "bash"\n', 'shell = "bash"\ncredentials = "connect"\n')
    )
    with pytest.raises(userconfig.ConfigError) as caught:
        settings.load()
    assert (
        str(caught.value) == f"{config_file}: [[agent.host]] credentials not recognized"
    )


def test_loading_reads_only_the_config(config_file: Path, tmp_path: Path) -> None:
    config_file.write_text(EXAMPLE)
    before = sorted(p.name for p in tmp_path.iterdir())
    settings.load()
    assert sorted(p.name for p in tmp_path.iterdir()) == before


def test_agent_is_registered_in_usage_and_dispatch() -> None:
    assert alpha.COMMANDS["agent"] is agent.main
    assert (
        "\n  agent        validate agent configuration (runs not yet available)\n"
        in alpha.USAGE
    )


@pytest.mark.parametrize(
    "run",
    [lambda: agent.main([], "example agent"), lambda: cli.main(["alpha", "agent"])],
)
def test_command_refuses_missing_configuration(
    config_file: Path, capsys: pytest.CaptureFixture[str], run: Callable[[], int]
) -> None:
    assert run() == 1
    out = capsys.readouterr()
    assert out.out == ""
    assert str(config_file) in out.err
    assert "config cannot be read" in out.err


def test_command_reports_a_config_error(
    config_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config_file.write_text('[agent]\nsockets_dir = "/x"\nzeta = 1\n')
    assert agent.main([], "example agent") == 1
    assert capsys.readouterr() == (
        "",
        f"example agent: {config_file}: [agent] zeta not recognized\n",
    )


def test_command_validates_and_refuses_to_run(
    config_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config_file.write_text(EXAMPLE)
    assert agent.main([], "example agent") == 1
    assert capsys.readouterr() == (
        "",
        "example agent: configuration valid; runs are not implemented\n",
    )


@pytest.mark.parametrize("argv", [["--help"], ["exec", "x"], ["-x"]])
def test_command_parses_no_options(
    argv: list[str], config_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config_file.write_text(EXAMPLE)
    assert agent.main(argv, "example agent") == 1
    assert capsys.readouterr().err == (
        "example agent: configuration valid; runs are not implemented\n"
    )

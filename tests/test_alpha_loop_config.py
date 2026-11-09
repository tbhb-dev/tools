"""Loop configuration uses synthetic values and never starts a runner."""

from __future__ import annotations

import json
from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from tbhbdev import alpha, cli, userconfig
from tbhbdev.alpha import loop
from tbhbdev.alpha.loop import config

if TYPE_CHECKING:
    from collections.abc import Callable

SETTINGS: dict[str, object] = {
    "repositories": ["example-org/example-repo", "example-org/another_repo.name"],
    "approval_command": ["example-approve", "--message", "two words", "$(literal)"],
    "store_path": "/example-state/loop.sqlite3",
    "reconcile_interval_minutes": 7,
    "spawn_cap_total": 8,
    "spawn_cap_per_host": 3,
    "daily_spawn_cap": 19,
    "hosts": ["example-host-a", "example-host-b"],
}
LIST_KEYS = ("repositories", "approval_command", "hosts")
INTEGER_KEYS = (
    "reconcile_interval_minutes",
    "spawn_cap_total",
    "spawn_cap_per_host",
    "daily_spawn_cap",
)


def write_settings(config_file: Path, settings: dict[str, object]) -> None:
    config_file.write_text(
        "[loop]\n"
        + "\n".join(f"{key} = {json.dumps(value)}" for key, value in settings.items())
        + "\n"
    )


def test_loads_all_settings_without_defaults(config_file: Path) -> None:
    write_settings(config_file, SETTINGS)
    assert config.load() == config.LoopConfig(
        repositories=("example-org/example-repo", "example-org/another_repo.name"),
        approval_command=("example-approve", "--message", "two words", "$(literal)"),
        store_path=Path("/example-state/loop.sqlite3"),
        reconcile_interval_minutes=7,
        spawn_cap_total=8,
        spawn_cap_per_host=3,
        daily_spawn_cap=19,
        hosts=("example-host-a", "example-host-b"),
    )


def test_result_is_frozen(config_file: Path) -> None:
    write_settings(config_file, SETTINGS)
    loaded = config.load()
    with pytest.raises(FrozenInstanceError):
        setattr(loaded, "daily_spawn_cap", 1)  # noqa: B010 -- test frozen assignment


def test_preserves_approval_arguments_and_absolute_store_path(
    config_file: Path,
) -> None:
    write_settings(config_file, SETTINGS)
    loaded = config.load()
    assert loaded.approval_command == (
        "example-approve",
        "--message",
        "two words",
        "$(literal)",
    )
    assert loaded.store_path == Path("/example-state/loop.sqlite3")


@pytest.mark.parametrize("key", SETTINGS)
def test_every_key_is_required(config_file: Path, key: str) -> None:
    write_settings(config_file, {k: v for k, v in SETTINGS.items() if k != key})
    with pytest.raises(userconfig.ConfigError) as caught:
        config.load()
    assert str(caught.value) == f"{config_file}: [loop] {key} is required"


@pytest.mark.parametrize("text", ["", "[unrelated]\nvalue = 1\n", "[loop]\n"])
def test_absent_table_is_optional_until_the_loop_needs_it(
    config_file: Path, text: str
) -> None:
    config_file.write_text(text)
    with pytest.raises(userconfig.ConfigError) as caught:
        config.load()
    assert str(caught.value) == f"{config_file}: [loop] repositories is required"


def test_missing_file_names_the_config(config_file: Path) -> None:
    with pytest.raises(userconfig.ConfigError) as caught:
        config.load()
    assert str(caught.value) == (
        f"{config_file}: config cannot be read: "
        f"{config_file} doesn't exist; create it and set [loop]"
    )
    assert not config_file.exists()


def test_bad_table_names_the_config(config_file: Path) -> None:
    config_file.write_text('loop = "invalid"\n')
    with pytest.raises(userconfig.ConfigError) as caught:
        config.load()
    assert str(caught.value) == (
        f"{config_file}: config cannot be read: {config_file}: loop must be a table"
    )


@pytest.mark.parametrize("text", [b"[loop", b"\xff"])
def test_invalid_file_is_a_configuration_error(config_file: Path, text: bytes) -> None:
    config_file.write_bytes(text)
    with pytest.raises(userconfig.ConfigError) as caught:
        config.load()
    assert str(caught.value).startswith(f"{config_file}: config cannot be read: ")
    assert "repositories" not in str(caught.value)


def test_unreadable_file_is_a_configuration_error(config_file: Path) -> None:
    config_file.mkdir()
    with pytest.raises(userconfig.ConfigError) as caught:
        config.load()
    assert str(caught.value).startswith(f"{config_file}: config cannot be read: ")
    assert "repositories" not in str(caught.value)


@pytest.mark.parametrize(
    ("key", "entry"),
    [("repositories", "example-org/example-repo"), ("hosts", "example-host-a")],
)
def test_repositories_and_hosts_reject_duplicates(
    config_file: Path, key: str, entry: str
) -> None:
    write_settings(config_file, {**SETTINGS, key: [entry] * 2})
    with pytest.raises(userconfig.ConfigError) as caught:
        config.load()
    assert (
        str(caught.value) == f"{config_file}: [loop] {key} must not contain duplicates"
    )


@pytest.mark.parametrize(
    "unknown", [{"spawn_cap_totals": 8}, {"z_unknown": 1, "a_unknown": 2}]
)
def test_unknown_keys_name_each_key(
    config_file: Path, unknown: dict[str, object]
) -> None:
    write_settings(config_file, {**SETTINGS, **unknown})
    with pytest.raises(userconfig.ConfigError) as caught:
        config.load()
    assert str(caught.value) == (
        f"{config_file}: [loop] {', '.join(sorted(unknown))} not recognized"
    )


@pytest.mark.parametrize("key", LIST_KEYS)
@pytest.mark.parametrize(
    "value", [1, "example", {}, [], [""], [" \t"], [1], ["example", False]]
)
def test_lists_require_non_blank_string_entries(
    config_file: Path, key: str, value: object
) -> None:
    write_settings(config_file, {**SETTINGS, key: value})
    with pytest.raises(userconfig.ConfigError) as caught:
        config.load()
    assert str(caught.value) == (
        f"{config_file}: [loop] {key} must be a non-empty list of non-blank strings"
    )


@pytest.mark.parametrize(
    "repo",
    [
        "example-repo",
        "/example-repo",
        "example-org/",
        "example-org/example-repo/extra",
        " example-org/example-repo",
        "example-org/example-repo\n",
        "https://example.invalid/example-org/example-repo",
    ],
)
def test_repositories_require_owner_and_name(config_file: Path, repo: str) -> None:
    write_settings(
        config_file,
        {**SETTINGS, "repositories": ["example-org/example-repo", repo]},
    )
    with pytest.raises(userconfig.ConfigError) as caught:
        config.load()
    assert str(caught.value) == (
        f"{config_file}: [loop] repositories must list owner/name strings"
    )


@pytest.mark.parametrize("key", INTEGER_KEYS)
@pytest.mark.parametrize("value", [0, -1, 1.5, "1", True, False, [], {}])
def test_intervals_and_caps_are_positive_integers(
    config_file: Path, key: str, value: object
) -> None:
    write_settings(config_file, {**SETTINGS, key: value})
    with pytest.raises(userconfig.ConfigError) as caught:
        config.load()
    assert (
        str(caught.value) == f"{config_file}: [loop] {key} must be a positive integer"
    )


def test_one_is_a_valid_interval_and_cap(config_file: Path) -> None:
    write_settings(config_file, {**SETTINGS, **dict.fromkeys(INTEGER_KEYS, 1)})
    loaded = config.load()
    assert loaded.reconcile_interval_minutes == 1
    assert loaded.spawn_cap_total == 1
    assert loaded.spawn_cap_per_host == 1
    assert loaded.daily_spawn_cap == 1


def test_per_host_cap_cannot_exceed_total(config_file: Path) -> None:
    write_settings(config_file, {**SETTINGS, "spawn_cap_per_host": 9})
    with pytest.raises(userconfig.ConfigError) as caught:
        config.load()
    assert str(caught.value) == (
        f"{config_file}: [loop] spawn_cap_per_host must not exceed spawn_cap_total"
    )


@pytest.mark.parametrize("value", [1, [], {}, "", " \t", "example\u0000path"])
def test_store_path_requires_a_path_string(config_file: Path, value: object) -> None:
    write_settings(config_file, {**SETTINGS, "store_path": value})
    with pytest.raises(userconfig.ConfigError) as caught:
        config.load()
    assert str(caught.value) == (
        f"{config_file}: [loop] store_path must be a non-blank path string without NUL"
    )


def test_load_reads_the_user_config_once(
    monkeypatch: pytest.MonkeyPatch, config_file: Path
) -> None:
    write_settings(config_file, SETTINGS)
    calls: list[Path] = []
    read_bytes = Path.read_bytes

    def read(path: Path) -> bytes:
        calls.append(path)
        return read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", read)
    config.load()
    assert calls == [config_file]


def test_store_path_expands_home(
    config_file: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    write_settings(
        config_file, {**SETTINGS, "store_path": "~/example-state/loop.sqlite3"}
    )
    assert config.load().store_path == tmp_path / "example-state" / "loop.sqlite3"


@pytest.mark.parametrize(
    "value", ["example-state/loop.sqlite3", ".", "../loop.sqlite3"]
)
def test_store_path_rejects_relative_input(config_file: Path, value: str) -> None:
    write_settings(config_file, {**SETTINGS, "store_path": value})
    with pytest.raises(userconfig.ConfigError) as caught:
        config.load()
    assert (
        str(caught.value)
        == f"{config_file}: [loop] store_path must be an absolute path"
    )


def test_loop_is_registered_in_usage_and_dispatch() -> None:
    assert alpha.COMMANDS["loop"] is loop.main
    assert "\n  loop " in alpha.USAGE


@pytest.mark.parametrize(
    "run", [lambda: loop.main([], "example loop"), lambda: cli.main(["alpha", "loop"])]
)
def test_command_refuses_missing_configuration(
    config_file: Path, capsys: pytest.CaptureFixture[str], run: Callable[[], int]
) -> None:
    assert run() == 1
    out = capsys.readouterr()
    assert out.out == ""
    assert str(config_file) in out.err
    assert "config cannot be read" in out.err


def test_command_only_loads_configuration(
    config_file: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    store = tmp_path / "example-state" / "loop.sqlite3"
    write_settings(config_file, {**SETTINGS, "store_path": str(store)})
    assert loop.main([], "example loop") == 1
    assert capsys.readouterr() == (
        "",
        (
            "example loop: configuration valid; shadow runner exists; "
            "live intake is not wired yet\n"
        ),
    )
    assert not store.parent.exists()


@pytest.mark.parametrize("flag", ["-h", "--help"])
def test_command_help_needs_no_configuration(
    flag: str, config_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as caught:
        loop.main([flag], "example loop")
    assert caught.value.code == 0
    assert capsys.readouterr() == (
        (
            "usage: example loop [-h]\n\n"
            "Validate loop configuration; live intake is not wired yet.\n\n"
            "options:\n"
            "  -h, --help  show this help message and exit\n"
        ),
        "",
    )
    assert not config_file.exists()


def test_command_refuses_arguments_before_loading_config(
    config_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as caught:
        loop.main(["start"], "example loop")
    assert caught.value.code == 2
    out = capsys.readouterr()
    assert out.out == ""
    assert "example loop: error: unrecognized arguments: start" in out.err
    assert str(config_file) not in out.err


@pytest.mark.parametrize("value", [False, True])
def test_retry_flag(config_file: Path, value: bool) -> None:
    write_settings(config_file, {**SETTINGS, "allow_claude_retry": value})
    assert config.load().allow_claude_retry is value


@pytest.mark.parametrize("value", [0, 1, "false", [], {}])
def test_retry_flag_rejects_non_boolean(config_file: Path, value: object) -> None:
    write_settings(config_file, {**SETTINGS, "allow_claude_retry": value})
    with pytest.raises(userconfig.ConfigError) as caught:
        config.load()
    assert (
        str(caught.value)
        == f"{config_file}: [loop] allow_claude_retry must be a boolean"
    )


@pytest.mark.parametrize("mode", ["off", "shadow", "on"])
def test_per_repository_modes(config_file: Path, mode: config.Mode) -> None:
    write_settings(config_file, SETTINGS)
    with config_file.open("a") as stream:
        stream.write(
            '\n[loop.rule_modes."example-org/example-repo"]\n'
            f'R0 = "{mode}"\nR11 = "{mode}"\n'
        )
    assert config.load().rule_modes == (
        config.RuleMode("example-org/example-repo", "R0", mode),
        config.RuleMode("example-org/example-repo", "R11", mode),
    )


@pytest.mark.parametrize(
    ("suffix", "why"),
    [
        ("rule_modes = 1", "must be a repository table"),
        (
            'rule_modes = {"example-org/unknown" = {}}',
            "must contain configured repository tables",
        ),
        (
            'rule_modes = {"example-org/example-repo" = 1}',
            "must contain configured repository tables",
        ),
        (
            'rule_modes = {"example-org/example-repo" = {R12 = "off"}}',
            "must contain R0-R11 modes off, shadow or on",
        ),
        (
            'rule_modes = {"example-org/example-repo" = {R1 = []}}',
            "must contain R0-R11 modes off, shadow or on",
        ),
        (
            'rule_modes = {"example-org/example-repo" = {R1 = true}}',
            "must contain R0-R11 modes off, shadow or on",
        ),
    ],
)
def test_invalid_rule_modes(config_file: Path, suffix: str, why: str) -> None:
    write_settings(config_file, SETTINGS)
    with config_file.open("a") as stream:
        stream.write(suffix + "\n")
    with pytest.raises(userconfig.ConfigError) as caught:
        config.load()
    assert str(caught.value) == f"{config_file}: [loop] rule_modes {why}"

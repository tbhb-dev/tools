"""Tests for tbhbdev.userconfig, against a config file in a temporary directory.

conftest.py points XDG_CONFIG_HOME at the test's scratch directory, so no
test reads the real user config.
"""

from __future__ import annotations

import tomllib
from typing import TYPE_CHECKING

import pytest

from tbhbdev import cli, userconfig

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

URL = "https://connect-a.example.ts.net"


def test_the_file_is_in_xdg_config_home(config_file: Path) -> None:
    assert userconfig.path() == config_file


@pytest.mark.parametrize("xdg", [None, ""])
def test_the_file_defaults_to_dot_config_in_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, xdg: str | None
) -> None:
    if xdg is None:
        monkeypatch.delenv("XDG_CONFIG_HOME")
    else:
        monkeypatch.setenv("XDG_CONFIG_HOME", xdg)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert userconfig.path() == tmp_path / ".config" / "tbhbdev" / "config.toml"


@pytest.mark.parametrize(
    ("table", "key", "value"),
    [
        ("op", "account", "example.1password.com"),
        ("connect", "server", "example-connect"),
        ("connect", "token_prefix", "example-host"),
        ("agent", "host_tag", "tag:example-agents"),
        ("vaults", "all", "vault-all"),
        ("vaults", "host_prefix", "vault-"),
    ],
)
def test_each_string_key_is_read(
    config: Callable[[list[str]], None], table: str, key: str, value: str
) -> None:
    config([URL])
    assert userconfig.string(table, key) == value


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("endpoints", [URL, "https://connect-b.example.ts.net"]),
        ("server_hosts", ["nas-a", "nas-b"]),
    ],
)
def test_each_list_key_is_read(
    config: Callable[[list[str]], None], key: str, value: list[str]
) -> None:
    config(value if key == "endpoints" else [URL])
    assert userconfig.strings("connect", key) == value


@pytest.mark.parametrize("read", [userconfig.string, userconfig.strings])
def test_a_missing_file_names_the_file_and_the_key(
    config_file: Path, read: Callable[[str, str], object]
) -> None:
    with pytest.raises(userconfig.ConfigError) as caught:
        read("op", "account")
    assert str(caught.value) == (
        f"{config_file} doesn't exist; create it and set [op] account"
    )


@pytest.mark.parametrize("text", ["", "op = 1\n", "[op]\n", '[op]\nother = "x"\n'])
def test_a_missing_key_names_the_file_and_the_key(config_file: Path, text: str) -> None:
    config_file.write_text(text)
    with pytest.raises(userconfig.ConfigError) as caught:
        userconfig.string("op", "account")
    assert str(caught.value) == f"{config_file} sets no [op] account"


@pytest.mark.parametrize("value", ["1", '""', '["a"]', "false"])
def test_a_string_of_the_wrong_type_is_an_error(config_file: Path, value: str) -> None:
    config_file.write_text(f"[op]\naccount = {value}\n")
    with pytest.raises(userconfig.ConfigError) as caught:
        userconfig.string("op", "account")
    assert str(caught.value) == (
        f"{config_file}: [op] account must be a non-empty string"
    )


@pytest.mark.parametrize("value", ['"a"', "[]", '["a", 1]', '["a", ""]', "{}"])
def test_a_list_of_the_wrong_type_is_an_error(config_file: Path, value: str) -> None:
    config_file.write_text(f"[connect]\nendpoints = {value}\n")
    with pytest.raises(userconfig.ConfigError) as caught:
        userconfig.strings("connect", "endpoints")
    assert str(caught.value) == (
        f"{config_file}: [connect] endpoints must be a non-empty list of strings"
    )


def test_a_table_is_read_whole(config_file: Path) -> None:
    config_file.write_text('[workspace]\nroot = "Code"\nrepos = ["a/b"]\n')
    assert userconfig.table("workspace") == {"root": "Code", "repos": ["a/b"]}


def test_a_missing_table_is_none(config_file: Path) -> None:
    config_file.write_text('[op]\naccount = "x"\n')
    assert userconfig.table("workspace") is None


def test_a_table_that_is_a_value_is_an_error(config_file: Path) -> None:
    config_file.write_text('workspace = "Code"\n')
    with pytest.raises(userconfig.ConfigError) as caught:
        userconfig.table("workspace")
    assert str(caught.value) == f"{config_file}: workspace must be a table"


def test_a_table_needs_the_file(config_file: Path) -> None:
    with pytest.raises(userconfig.ConfigError) as caught:
        userconfig.table("workspace")
    assert str(caught.value) == (
        f"{config_file} doesn't exist; create it and set [workspace]"
    )


def test_a_file_of_bad_toml_is_an_error(config_file: Path) -> None:
    config_file.write_text("[op\n")
    with pytest.raises(userconfig.ConfigError) as caught:
        userconfig.string("op", "account")
    assert str(caught.value).startswith(f"cannot parse {config_file}: ")


def test_the_file_is_read_as_utf8(config_file: Path) -> None:
    config_file.write_bytes('[op]\naccount = "café"\n'.encode())
    assert userconfig.string("op", "account") == "café"


# Rendering the file with one value set.


def test_render_sets_a_new_key_after_the_tables_others(config_file: Path) -> None:
    config_file.write_text(
        "# The operator's config.\n"
        '[op]\naccount = "example.1password.com"  # a comment\n'
        "[connect]\n"
        'server_hosts = ["nas-a", "nas-b"]\n'
        "[vaults]\n"
        'all = "vault-all"\n'
    )
    assert userconfig.render("connect", "endpoints", [URL, "https://b"]) == (
        '[op]\naccount = "example.1password.com"\n\n'
        '[connect]\nserver_hosts = ["nas-a", "nas-b"]\n'
        f'endpoints = ["{URL}", "https://b"]\n\n'
        '[vaults]\nall = "vault-all"\n'
    )


def test_render_replaces_a_key_where_it_is(config_file: Path) -> None:
    config_file.write_text(
        '[connect]\nendpoints = ["https://old"]\nserver_hosts = []\n'
    )
    assert userconfig.render("connect", "endpoints", [URL]) == (
        f'[connect]\nendpoints = ["{URL}"]\nserver_hosts = []\n'
    )


def test_render_adds_a_missing_table_last(config_file: Path) -> None:
    config_file.write_text('[op]\naccount = "a"\n[empty]\n')
    assert userconfig.render("connect", "endpoints", [URL]) == (
        f'[op]\naccount = "a"\n\n[empty]\n\n[connect]\nendpoints = ["{URL}"]\n'
    )


def test_render_quotes_keys_and_escapes_strings(config_file: Path) -> None:
    config_file.write_text(
        '["a b"]\n'
        "Key_1-x = 'q\"b\\'\n"
        '"é" = "line\\none\\ttab\\u0001\\u007fdel"\n'
        '"café" = ["ü"]\n'
    )
    text = userconfig.render("t", "k", [])
    assert text == (
        '["a b"]\n'
        'Key_1-x = "q\\"b\\\\"\n'
        '"é" = "line\\none\\ttab\\u0001\\u007fdel"\n'
        '"café" = ["ü"]\n\n'
        "[t]\nk = []\n"
    )
    assert tomllib.loads(text) == {
        "a b": {
            "Key_1-x": 'q"b\\',
            "é": "line\none\ttab\x01\x7fdel",
            "café": ["ü"],
        },
        "t": {"k": []},
    }


@pytest.mark.parametrize(
    ("text", "error"),
    [
        ("x = 1\n", "x must be a table"),
        ("[op]\nport = 1\n", "[op] port must be a string or a list of strings"),
        ('[op]\nhosts = ["a", 1]\n', "[op] hosts must be a string or a list of"),
        ("[op.inner]\nx = 'a'\n", "[op] inner must be a string or a list of"),
    ],
)
def test_render_refuses_other_values(config_file: Path, text: str, error: str) -> None:
    config_file.write_text(text)
    with pytest.raises(userconfig.ConfigError) as caught:
        userconfig.render("connect", "endpoints", [URL])
    assert str(caught.value).startswith(f"{config_file}: {error}")


def test_render_needs_the_file(config_file: Path) -> None:
    with pytest.raises(userconfig.ConfigError) as caught:
        userconfig.render("connect", "endpoints", [URL])
    assert str(caught.value) == (
        f"{config_file} doesn't exist; create it and set [connect] endpoints"
    )


@pytest.mark.parametrize(
    "argv", [["--help"], ["host", "--help"], ["host", "status", "--help"]]
)
def test_help_works_without_the_file(
    config_file: Path, argv: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    assert not config_file.exists()
    assert cli.main(argv) == 0
    assert "usage:" in capsys.readouterr().out


def test_the_optional_readers_return_none_without_a_file_table_or_key(
    config_file: Path,
) -> None:
    assert userconfig.optional_table("harness") is None
    assert userconfig.optional_string("harness", "rule") is None
    assert userconfig.optional_strings("harness", "hosts") is None
    config_file.write_text("[other]\nkey = 'x'\n")
    assert userconfig.optional_table("harness") is None
    assert userconfig.optional_string("harness", "rule") is None
    assert userconfig.optional_strings("harness", "hosts") is None
    config_file.write_text("[harness]\nother = 'x'\n")
    assert userconfig.optional_table("harness") == {"other": "x"}
    assert userconfig.optional_string("harness", "rule") is None
    assert userconfig.optional_strings("harness", "hosts") is None


def test_the_optional_readers_return_a_value_the_file_sets(config_file: Path) -> None:
    config_file.write_text("[harness]\nrule = 'r'\nhosts = ['a=b', 'c']\n")
    assert userconfig.optional_string("harness", "rule") == "r"
    assert userconfig.optional_strings("harness", "hosts") == ["a=b", "c"]


def test_the_optional_readers_reject_a_bad_value_and_a_bad_table(
    config_file: Path,
) -> None:
    config_file.write_text("[harness]\nrule = ''\nhosts = [1]\n")
    with pytest.raises(userconfig.ConfigError, match=r"\[harness\] rule must be"):
        userconfig.optional_string("harness", "rule")
    with pytest.raises(userconfig.ConfigError, match=r"\[harness\] hosts must be"):
        userconfig.optional_strings("harness", "hosts")
    config_file.write_text("harness = 'x'\n")
    with pytest.raises(userconfig.ConfigError, match="harness must be a table"):
        userconfig.optional_string("harness", "rule")

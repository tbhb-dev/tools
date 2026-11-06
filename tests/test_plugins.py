"""Tests for tbhbdev.plugins: tools and config files from entry points."""

from __future__ import annotations

from importlib.metadata import EntryPoint
from typing import TYPE_CHECKING

import pytest

from tbhbdev import plugins

if TYPE_CHECKING:
    from conftest import Installed


def tool(_argv: list[str], _prog: str) -> int:
    return 0


def other(_argv: list[str], _prog: str) -> int:
    return 1


BUILTINS = {"built-in": tool}


def test_no_plugin_leaves_the_built_ins() -> None:
    assert plugins.load() == ()
    assert plugins.tools(BUILTINS) == BUILTINS


def test_the_real_lookup_reads_the_plugin_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    groups: list[str] = []

    def entry_points(*, group: str) -> list[EntryPoint]:
        groups.append(group)
        return []

    monkeypatch.setattr(plugins, "entry_points", entry_points)
    plugins.load.cache_clear()
    assert plugins.load() == ()
    assert groups == ["tbhbdev.plugins"]


def test_plugins_load_once(installed: Installed) -> None:
    installed.stub("one")
    first = plugins.load()
    installed.points.clear()
    assert plugins.load() is first


def test_plugin_tools_join_the_built_ins(installed: Installed) -> None:
    installed.stub("one", tools={"a": tool})
    installed.add("bare", object())
    installed.stub("two", tools={"b": other})
    assert plugins.tools(BUILTINS) == {"built-in": tool, "a": tool, "b": other}


def test_a_tool_that_shadows_a_built_in_is_an_error(installed: Installed) -> None:
    installed.stub("one", tools={"built-in": other})
    with pytest.raises(
        plugins.PluginError,
        match="plugin 'one' defines the command 'built-in', "
        "which the built-in commands already define",
    ):
        plugins.tools(BUILTINS)


def test_a_tool_two_plugins_define_is_an_error(installed: Installed) -> None:
    installed.stub("one", tools={"a": tool})
    installed.stub("two", tools={"a": other})
    with pytest.raises(
        plugins.PluginError,
        match="plugin 'two' defines the command 'a', which plugin 'one' already",
    ):
        plugins.tools(BUILTINS)


def test_a_plugin_that_fails_to_import_is_reported_and_skipped(
    installed: Installed, capsys: pytest.CaptureFixture[str]
) -> None:
    missing = "tbhbdev_no_such_plugin_module"
    installed.points.append(EntryPoint("broken", missing, plugins.GROUP))
    installed.stub("good", tools={"a": tool})
    installed.add("raises", RuntimeError("boom"))
    assert plugins.tools(BUILTINS) == {"built-in": tool, "a": tool}
    assert [name for name, _ in plugins.load()] == ["good"]
    assert capsys.readouterr().err == (
        "tbhbdev: cannot load plugin 'broken': "
        f"ModuleNotFoundError: No module named {missing!r}\n"
        "tbhbdev: cannot load plugin 'raises': RuntimeError: boom\n"
    )


def test_config_falls_back_to_the_package_file() -> None:
    # The package ships no config files, so the fallback finds none.
    with pytest.raises(FileNotFoundError, match=r"config/secrets\.toml"):
        plugins.config("secrets.toml")


def test_config_prefers_the_plugin_that_supplies_the_file(
    installed: Installed,
) -> None:
    installed.add("bare", object())
    installed.stub("other", files={"other.toml": "x"})
    installed.stub("one", files={"secrets.toml": "[connect]\n"})
    assert plugins.config("secrets.toml") == "[connect]\n"


def test_config_two_plugins_supply_is_an_error(installed: Installed) -> None:
    installed.stub("one", files={"secrets.toml": "a"})
    installed.stub("two", files={"secrets.toml": "b"})
    with pytest.raises(
        plugins.PluginError, match=r"plugins 'one', 'two' each supply secrets\.toml"
    ):
        plugins.config("secrets.toml")

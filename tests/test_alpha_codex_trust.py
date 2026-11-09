"""Tests for tbhbdev.alpha.codex_trust: Codex's trusted-hook keys and hashes."""

import hashlib
import tomllib
from pathlib import Path
from typing import cast

import pytest

from tbhbdev.alpha import codex_trust

HOOKS = Path("/home/agent/.codex/hooks.json")
KEY = f"{HOOKS}:pre_tool_use:0:0"


def sha(compact: str) -> str:
    return f"sha256:{hashlib.sha256(compact.encode()).hexdigest()}"


def is_mine(entry: object) -> bool:
    return isinstance(entry, dict) and str(
        cast("dict[str, object]", entry)["command"]
    ).endswith("# mine")


def mine(name: str) -> dict[str, object]:
    return {"type": "command", "command": f"{name} # mine", "timeout": 600}


THEIRS = {"type": "command", "command": "theirs"}


def hooks_of(text: str) -> dict[str, object]:
    return cast("dict[str, object]", tomllib.loads(text)["hooks"]["state"])


# ------------------------------------------------------------------ hashes


def test_the_golden_vector() -> None:
    assert codex_trust.hook_hash("pre_tool_use", "Bash", "example-hook run", 10) == (
        "sha256:650e0f6b5aba6ef9c77fe9cc1874666d7f0be18194a90f58c4e4106c8a50d37d"
    )


@pytest.mark.parametrize(
    ("matcher", "compact"),
    [
        (
            "Bash",
            (
                '{"event_name":"stop","hooks":[{"async":false,"command":"c",'
                '"timeout":600,"type":"command"}],"matcher":"Bash"}'
            ),
        ),
        (
            None,
            (
                '{"event_name":"stop","hooks":[{"async":false,"command":"c",'
                '"timeout":600,"type":"command"}]}'
            ),
        ),
    ],
)
def test_hook_hash_is_sha256_of_compact_sorted_json(
    matcher: str | None, compact: str
) -> None:
    assert codex_trust.hook_hash("stop", matcher, "c", 600) == sha(compact)


@pytest.mark.parametrize(
    ("event", "label"),
    [
        ("PreToolUse", "pre_tool_use"),
        ("PostToolUse", "post_tool_use"),
        ("SessionStart", "session_start"),
        ("SubagentStart", "subagent_start"),
        ("Stop", "stop"),
    ],
)
def test_event_label(event: str, label: str) -> None:
    assert codex_trust.event_label(event) == label


@pytest.mark.parametrize(
    ("label", "group", "handler", "key"),
    [
        ("pre_tool_use", 0, 0, f"{HOOKS}:pre_tool_use:0:0"),
        ("session_start", 2, 1, f"{HOOKS}:session_start:2:1"),
        ("post_tool_use", 10, 12, f"{HOOKS}:post_tool_use:10:12"),
    ],
)
def test_hook_key(label: str, group: int, handler: int, key: str) -> None:
    assert codex_trust.hook_key(HOOKS, label, group, handler) == key


# --------------------------------------------------------------- our_hashes


def test_our_hashes_trusts_only_our_handlers_among_foreign_groups() -> None:
    events = {
        "PreToolUse": [
            {"matcher": "Bash", "hooks": [THEIRS]},
            {"matcher": "Bash", "hooks": [mine("one")]},
            {"matcher": "Edit", "hooks": [THEIRS, mine("two"), THEIRS, mine("three")]},
            {"matcher": "Bash", "hooks": [THEIRS]},
        ],
        "SessionStart": [{"hooks": [mine("four")]}],
        "Stop": [{"hooks": [THEIRS]}],
    }
    found = codex_trust.our_hashes(HOOKS, {"hooks": events}, is_mine)
    assert found == {
        f"{HOOKS}:pre_tool_use:1:0": codex_trust.hook_hash(
            "pre_tool_use", "Bash", "one # mine", 600
        ),
        f"{HOOKS}:pre_tool_use:2:1": codex_trust.hook_hash(
            "pre_tool_use", "Edit", "two # mine", 600
        ),
        f"{HOOKS}:pre_tool_use:2:3": codex_trust.hook_hash(
            "pre_tool_use", "Edit", "three # mine", 600
        ),
        f"{HOOKS}:session_start:0:0": codex_trust.hook_hash(
            "session_start", None, "four # mine", 600
        ),
    }


def test_our_hashes_counts_odd_groups_and_skips_them() -> None:
    events = {
        "Stop": "not a list",
        "PreToolUse": [
            "not a group",
            {"matcher": "Bash"},
            {"matcher": "Bash", "hooks": "not a list"},
            {"matcher": "Bash", "hooks": ["not a handler", mine("one")]},
        ],
    }
    assert codex_trust.our_hashes(HOOKS, {"hooks": events}, is_mine) == {
        f"{HOOKS}:pre_tool_use:3:1": codex_trust.hook_hash(
            "pre_tool_use", "Bash", "one # mine", 600
        )
    }


@pytest.mark.parametrize(
    "config",
    [
        {},
        {"hooks": "not an object"},
        {"hooks": {}},
        {"hooks": {"PreToolUse": "not a list"}},
    ],
)
def test_our_hashes_of_nothing_is_empty(config: dict[str, object]) -> None:
    assert codex_trust.our_hashes(HOOKS, config, is_mine) == {}


# ------------------------------------------------------------- trusted_text


def test_trusted_text_makes_the_file() -> None:
    assert codex_trust.trusted_text(None, HOOKS, {KEY: "sha256:aa"}) == (
        f'[hooks.state."{KEY}"]\ntrusted_hash = "sha256:aa"\n'
    )
    assert codex_trust.trusted_text("", HOOKS, {KEY: "sha256:aa"}) == (
        f'[hooks.state."{KEY}"]\ntrusted_hash = "sha256:aa"\n'
    )


def test_trusted_text_keeps_comments_tables_and_foreign_entries() -> None:
    other = "/other/hooks.json:pre_tool_use:0:0"
    old = f"""# my codex config
model = "gpt" # the model

[mcp_servers.x]
command = "x"  # keep

[hooks.state."{other}"]
trusted_hash = "sha256:foreign"
enabled = false

[profiles.y]
a = [1, 2]
"""
    new = codex_trust.trusted_text(old, HOOKS, {KEY: "sha256:aa"})
    for line in old.splitlines():
        assert line in new.splitlines()
    parsed = tomllib.loads(new)
    expected = tomllib.loads(old)
    expected["hooks"]["state"][KEY] = {"trusted_hash": "sha256:aa"}
    assert parsed == expected


def test_trusted_text_is_idempotent() -> None:
    wanted = {KEY: "sha256:aa", f"{HOOKS}:stop:0:0": "sha256:bb"}
    old = '# c\nx = 1\n\n[hooks.state."/other:a:0:0"]\ntrusted_hash = "sha256:f"\n'
    once = codex_trust.trusted_text(old, HOOKS, wanted)
    assert codex_trust.trusted_text(once, HOOKS, wanted) == once
    assert once != old


def test_trusted_text_updates_a_changed_hash_in_place() -> None:
    old = f'[hooks.state."{KEY}"]\ntrusted_hash = "sha256:old"\nenabled = false\n'
    new = codex_trust.trusted_text(old, HOOKS, {KEY: "sha256:aa"})
    assert hooks_of(new) == {KEY: {"trusted_hash": "sha256:aa", "enabled": False}}


def test_trusted_text_leaves_a_matching_entry_as_written() -> None:
    old = f"[hooks.state.\"{KEY}\"]\ntrusted_hash = 'sha256:aa' # mine\n"
    assert codex_trust.trusted_text(old, HOOKS, {KEY: "sha256:aa"}) == old


def test_trusted_text_updates_an_inline_entry() -> None:
    old = f'[hooks.state]\n"{KEY}" = {{ trusted_hash = "sha256:old" }}\n'
    new = codex_trust.trusted_text(old, HOOKS, {KEY: "sha256:aa"})
    assert hooks_of(new) == {KEY: {"trusted_hash": "sha256:aa"}}


def test_trusted_text_replaces_an_entry_that_is_not_a_table() -> None:
    new = codex_trust.trusted_text(f'[hooks.state]\n"{KEY}" = 5\n', HOOKS, {KEY: "x"})
    assert hooks_of(new) == {KEY: {"trusted_hash": "x"}}


def test_trusted_text_works_in_an_existing_hooks_table() -> None:
    new = codex_trust.trusted_text("[hooks]\nfoo = 1\n", HOOKS, {KEY: "sha256:aa"})
    assert tomllib.loads(new) == {
        "hooks": {"foo": 1, "state": {KEY: {"trusted_hash": "sha256:aa"}}}
    }


def test_trusted_text_edits_hook_tables_that_other_tables_interrupt() -> None:
    # Codex appends a `[hooks.state."..."]` for a hook trusted in /hooks, after
    # whatever else the file holds, so hooks and hooks.state are out of order.
    other = f"{HOOKS}:stop:0:0"
    old = f"""[hooks.state."{KEY}"]
trusted_hash = "sha256:old"

[shell_environment_policy.set]
A = "1"

[hooks.state."/elsewhere/hooks.json:stop:0:0"]
trusted_hash = "sha256:theirs"
"""
    new = codex_trust.trusted_text(old, HOOKS, {KEY: "sha256:aa", other: "sha256:bb"})
    assert tomllib.loads(new) == {
        "hooks": {
            "state": {
                KEY: {"trusted_hash": "sha256:aa"},
                other: {"trusted_hash": "sha256:bb"},
                "/elsewhere/hooks.json:stop:0:0": {"trusted_hash": "sha256:theirs"},
            }
        },
        "shell_environment_policy": {"set": {"A": "1"}},
    }
    assert (
        codex_trust.trusted_text(new, HOOKS, {KEY: "sha256:aa", other: "sha256:bb"})
        == new
    )


def test_trusted_text_removes_our_hash_from_an_old_position() -> None:
    old_key = f"{HOOKS}:pre_tool_use:0:0"
    new_key = f"{HOOKS}:pre_tool_use:1:0"
    old = f"""[hooks.state."{old_key}"]
trusted_hash = "sha256:aa"

[hooks.state."{HOOKS}:stop:0:0"]
trusted_hash = "sha256:aa"
"""
    new = codex_trust.trusted_text(
        old, HOOKS, {new_key: "sha256:aa", f"{HOOKS}:stop:0:0": "sha256:aa"}
    )
    assert hooks_of(new) == {
        f"{HOOKS}:stop:0:0": {"trusted_hash": "sha256:aa"},
        new_key: {"trusted_hash": "sha256:aa"},
    }


def test_trusted_text_removes_an_inline_entry_at_an_old_position() -> None:
    old = f'[hooks.state]\n"{KEY}" = {{ trusted_hash = "sha256:aa" }}\n'
    new = codex_trust.trusted_text(old, HOOKS, {f"{HOOKS}:stop:0:0": "sha256:aa"})
    assert hooks_of(new) == {f"{HOOKS}:stop:0:0": {"trusted_hash": "sha256:aa"}}


@pytest.mark.parametrize(
    "stays",
    [
        # Another hooks.json, even with our hash.
        ("/other/hooks.json:pre_tool_use:0:0", "sha256:aa"),
        # Someone else's hook at a position of this hooks.json.
        (f"{HOOKS}:pre_tool_use:0:0", "sha256:theirs"),
        # Not a key of the right shape.
        (f"{HOOKS}:pre_tool_use:0", "sha256:aa"),
        (str(HOOKS), "sha256:aa"),
        (f"{HOOKS}.bak:pre_tool_use:0:0", "sha256:aa"),
        # A path with a colon in it, which is not this one.
        (f"/x:{HOOKS}:pre_tool_use:0:0", "sha256:aa"),
    ],
)
def test_trusted_text_keeps_every_other_entry(stays: tuple[str, str]) -> None:
    key, digest = stays
    old = f'[hooks.state."{key}"]\ntrusted_hash = "{digest}"\n'
    new = codex_trust.trusted_text(old, HOOKS, {f"{HOOKS}:stop:0:0": "sha256:aa"})
    assert hooks_of(new)[key] == {"trusted_hash": digest}


@pytest.mark.parametrize("value", ['"not a table"', "{ enabled = false }", "{}"])
def test_trusted_text_keeps_a_stale_looking_entry_without_a_hash(value: str) -> None:
    old = f'[hooks.state]\n"{KEY}" = {value}\n'
    new = codex_trust.trusted_text(old, HOOKS, {f"{HOOKS}:stop:0:0": "sha256:aa"})
    assert hooks_of(new)[KEY] == tomllib.loads(old)["hooks"]["state"][KEY]


def test_trusted_text_matches_a_path_with_a_colon() -> None:
    path = Path("/a:b/hooks.json")
    key = f"{path}:pre_tool_use:0:0"
    old = f'[hooks.state."{key}"]\ntrusted_hash = "sha256:aa"\n'
    new = codex_trust.trusted_text(old, path, {f"{path}:stop:0:0": "sha256:aa"})
    assert list(hooks_of(new)) == [f"{path}:stop:0:0"]


def test_trusted_text_never_removes_a_key_it_is_trusting() -> None:
    old = f"""[hooks.state."{KEY}"]
trusted_hash = "sha256:aa"

[hooks.state."/z"]
trusted_hash = "sha256:z"
"""
    assert codex_trust.trusted_text(old, HOOKS, {KEY: "sha256:aa"}) == old


@pytest.mark.parametrize(
    ("old", "message"),
    [
        ("hooks = 3\n", "its hooks is not a table"),
        ("[hooks]\nstate = 3\n", "its hooks.state is not a table"),
        ("hooks = { state = {} }\n", "its hooks is not a table"),
        ("[hooks\n", "it is not valid TOML ("),
        ("x = = 1\n", "it is not valid TOML ("),
    ],
)
def test_trusted_text_refuses_a_file_it_cannot_edit(old: str, message: str) -> None:
    with pytest.raises(ValueError, match=r"^it") as caught:
        codex_trust.trusted_text(old, HOOKS, {KEY: "sha256:aa"})
    assert str(caught.value).startswith(message)
    assert "\n" not in str(caught.value)

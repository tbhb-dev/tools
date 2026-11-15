# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns
"""Tests for tbhbdev alpha state-guard, against a config in a temporary XDG dir."""

import io
import json
from collections.abc import Callable
lazy from pathlib import Path

import pytest

from tbhbdev.alpha import state_guard

CONFIG = """[harness]
rule = "Example rule: claims carry evidence."
decisions_dir = "/example/decisions"
open_note = "/example/Open.md"
"""
UNSUPPORTED_CLAIM = "The migration was applied to example-host."
TRAILER_MESSAGE = (
    "state-guard: agent commits carry no Signed-off-by or Co-authored-by "
    "trailer and no -s or --signoff. Remove it.\n"
)

type Result = tuple[int, str, str]
type Runner = Callable[[object], Result]


@pytest.fixture
def run(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> Runner:
    def call(data: object) -> Result:
        monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(data)))
        status = state_guard.main()
        out = capsys.readouterr()
        return status, out.out, out.err

    return call


@pytest.fixture
def _configured(config_file: Path) -> None:
    config_file.write_text(CONFIG)


def send(message: str) -> dict[str, object]:
    return {
        "hook_event_name": "PreToolUse",
        "tool_name": "SendMessage",
        "tool_input": {"message": message},
    }


def bash(command: str) -> dict[str, object]:
    return {
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": command},
    }


def write(tool: str, path: str, **fields: str) -> dict[str, object]:
    return {
        "hook_event_name": "PreToolUse",
        "tool_name": tool,
        "tool_input": {"file_path": path, **fields},
    }


def stop(**fields: object) -> dict[str, object]:
    return {"hook_event_name": "Stop", **fields}


@pytest.mark.parametrize("text", [None, "", "[other]\nkey = 1\n"])
def test_the_commit_check_runs_without_config(
    config_file: Path, run: Runner, text: str | None
) -> None:
    config_file.unlink(missing_ok=True)
    if text is not None:
        config_file.write_text(text)
    assert run(bash("git commit -s -m x")) == (2, "", TRAILER_MESSAGE)


def test_claims_in_messages_and_replies_pass(run: Runner) -> None:
    assert run(send(UNSUPPORTED_CLAIM)) == (0, "", "")
    assert run(stop(last_assistant_message=UNSUPPORTED_CLAIM)) == (0, "", "")


@pytest.mark.parametrize("text", [None, "", "[other]\nkey = 1\n"])
def test_the_checks_that_need_one_go_silent_without_config(
    config_file: Path, run: Runner, text: str | None
) -> None:
    if text is not None:
        config_file.write_text(text)
    assert run({"hook_event_name": "SessionStart"}) == (0, "", "")
    note = write("Write", "/example/Open.md", content="- **a** x")
    assert run(note) == (0, "", "")


def test_an_invalid_config_allows_and_says_why(config_file: Path, run: Runner) -> None:
    config_file.write_text('[harness]\nrule = ""\n')
    status, out, err = run({"hook_event_name": "SessionStart"})
    assert (status, out) == (0, "")
    assert err.startswith("state-guard: ignoring the config: ")
    assert "[harness] rule must be a non-empty string" in err


def test_a_crash_allows(run: Runner, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(_data: object) -> int:
        raise RuntimeError

    monkeypatch.setattr(state_guard, "run", boom)
    assert run(bash("git status")) == (0, "", "")


@pytest.mark.usefixtures("_configured")
def test_session_start_prints_the_rule(run: Runner) -> None:
    assert run({"hook_event_name": "SessionStart"}) == (
        0,
        "Example rule: claims carry evidence.\n",
        "",
    )


def test_session_start_without_a_rule_prints_nothing(
    config_file: Path, run: Runner
) -> None:
    config_file.write_text('[harness]\ndecisions_dir = "/example/decisions"\n')
    assert run({"hook_event_name": "SessionStart"}) == (0, "", "")


def test_other_tools_and_odd_inputs_pass(run: Runner) -> None:
    for data in (
        {
            "hook_event_name": "PreToolUse",
            "tool_name": "Read",
            "tool_input": dict[str, object](),
        },
        {"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": None},
        dict[str, object](),
    ):
        assert run(data) == (0, "", "")


# ------------------------------------------------------------------ commits


@pytest.mark.parametrize(
    "command",
    [
        'git commit -m "x\n\nSigned-off-by: A <a@example.com>"',
        "git commit --signoff -m x",
        "git commit --trailer 'Co-authored-by: A <a@example.com>' -m x",
        "git add . && git commit --trailer=signed-off-by:x -m x",
        "git commit -s -m x",
        "git commit -m x -s",
        "git commit -as -m x",
        "git commit -sm x",
        "env X=1 git -C d commit -s",
        "git --no-pager commit -s",
        "command git commit -s",
        "exec git commit -s",
        "X=1 Y=2 git commit -s",
        "git -c user.name=x -C d commit -s",
        "/usr/bin/git commit -s",
        "true && git commit --signoff",
        "true || git commit -s",
        "true; git commit -s",
        "true | git commit -s",
        "true & git commit -s",
        "echo hi\ngit commit -s",
        "$(git commit -s)",
        "x=$(git commit -s)",
        "(git commit -s)",
        "{ git commit -s; }",
        "echo `git commit -s`",
        "echo 'a;b' && git commit -s",
        'echo "a;b" && git commit -s',
        "echo \\' && git commit -s",
        "cat <<EOF\nhi\nEOF\ngit commit -s",
        'grep a <<< "x"\ngit commit -s -m y',
        'grep a <<<< "x"\ngit commit -s -m y',
        "git commit -m x <<EOF\nEOF\ngit commit -s",
    ],
)
def test_a_signing_commit_is_blocked(run: Runner, command: str) -> None:
    assert run(bash(command)) == (2, "", TRAILER_MESSAGE)


@pytest.mark.parametrize(
    "command",
    [
        "cat <<EOF\ngit commit -s\nEOF",
        "cat <<'EOF'\ngit commit -s\nEOF",
        'cat <<"EOF"\ngit commit -s\nEOF',
        "cat <<-EOF\ngit commit -s\n\tEOF",
        "cat <<A <<B\ngit commit -s\nA\ngit commit --signoff\nB\ngit status",
        'echo "git commit -s"',
        "echo 'git commit --signoff'",
        'echo "a && git commit -s"',
        'echo \\" git commit -s',
        "echo '\\' ; git status",
        "git commit -m 'fix -s handling'",
        'git commit -m "a -s b"',
        "git commit -m -s",
        "git commit -F -s",
        "git commit -am x",
        "git commit -S -m x",
        "git commit --no-signoff -m x",
        "git commit --no-verify -m x",
        "git commit --message=x",
        "git commit",
        "git status && echo Signed-off-by: x",
        "git -C",
        "git -C d status -s",
        "git log -s",
        "git push --signoff",
        "git",
        "git-extra commit -s",
        "ls -s",
        "env",
        "",
        "echo 'unterminated && git commit -s",
        "FOO=1",
    ],
)
def test_other_commands_pass(run: Runner, command: str) -> None:
    assert run(bash(command)) == (0, "", "")


C = "git" + " commit"  # the shared command-matching table
COMMAND_TABLE = {
    f"{C} -s -m x": 2,
    f"cd r && {C} -s -m x": 2,
    "git -C repo commit --signoff -m x": 2,
    f"FOO=1 {C} -sm x": 2,
    f"{C} -m 'Signed-off-by: a'": 2,
    f"echo $({C} -s -m x)": 2,
    f"cat > t.py <<'EOF'\nassert check('{C} -s -m x')\nEOF\necho done": 0,
    f"cat > t.py <<EOF\n{C} --signoff\nEOF": 0,
    f"{C} --no-signoff -m 'fix: thing'": 0,
    f"grep -n '{C} -s' file": 0,
    "git log --max-count=1": 0,
}


@pytest.mark.parametrize(("command", "status"), COMMAND_TABLE.items())
def test_the_shared_command_matching_table(
    run: Runner, command: str, status: int
) -> None:
    assert run(bash(command))[0] == status


def test_a_value_flag_hides_only_its_own_value(run: Runner) -> None:
    assert run(bash("git commit -m -s -s"))[0] == 2
    assert run(bash("git commit -m x -m -s"))[0] == 0
    assert run(bash("git commit --message -s --file -s"))[0] == 0
    assert run(bash("git commit -C HEAD -c x -t y"))[0] == 0


# ---------------------------------------------------------------- open note


@pytest.mark.usefixtures("_configured")
def test_a_new_open_item_without_a_decisions_check_is_blocked(run: Runner) -> None:
    other = write("Write", "/example/notes/Other.md", content="- **Item** x")
    assert run(other) == (0, "", "")
    status, _, err = run(write("Write", "/example/Open.md", content="- **Item** x"))
    assert status == 2
    assert err == (
        "state-guard: a new open item needs 'Decisions checked: <grep terms>', "
        "showing you searched /example/decisions first.\n"
    )


@pytest.mark.usefixtures("_configured")
def test_an_edit_adding_an_item_is_blocked_and_an_old_item_is_not(run: Runner) -> None:
    note = "/example/Open.md"
    added = write("Edit", note, old_string="- **a** x", new_string="- **b** y")
    assert run(added)[0] == 2
    kept = write("Edit", note, old_string="- **a** x", new_string="- **a** x\ntext")
    assert run(kept) == (0, "", "")
    bare = write("Edit", note, new_string="- **a** x")
    assert run(bare)[0] == 2


@pytest.mark.usefixtures("_configured")
def test_a_checked_item_and_a_note_without_items_pass(run: Runner) -> None:
    note = "/example/Open.md"
    checked = write("Write", note, content="- **a** x\nDecisions checked: foo")
    assert run(checked) == (0, "", "")
    assert run(write("Write", note, content="prose only")) == (0, "", "")
    assert run(write("Write", note)) == (0, "", "")
    no_path = {
        "hook_event_name": "PreToolUse",
        "tool_name": "Write",
        "tool_input": dict[str, object](),
    }
    assert run(no_path) == (0, "", "")


@pytest.mark.parametrize(
    "text",
    [
        '[harness]\nopen_note = "/example/Open.md"\n',
        '[harness]\ndecisions_dir = "/example/decisions"\n',
    ],
)
def test_the_open_note_check_needs_both_keys(
    config_file: Path, run: Runner, text: str
) -> None:
    config_file.write_text(text)
    data = write("Write", "/example/Open.md", content="- **a** x")
    assert run(data) == (0, "", "")


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        (r"echo a\;b; git commit -s", [r"echo a\;b", " git commit -s"]),
        (r"echo a\&b && git commit -s", [r"echo a\&b ", "", " git commit -s"]),
        ("echo 'a\\'; git commit -s", ["echo 'a\\'", " git commit -s"]),
        ("echo X; git commit -s", ["echo X", " git commit -s"]),
        ("echo \\'", ["echo \\'"]),
        ("echo \\", ["echo \\"]),
    ],
)
def test_shell_splitting_preserves_escaped_text(
    command: str, expected: list[str]
) -> None:
    assert state_guard.simple_commands(command) == expected


def test_a_trailer_can_span_argument_boundaries() -> None:
    assert state_guard.signs_off(["Signed-off-by", ": Example"])


@pytest.mark.usefixtures("_configured")
def test_write_does_not_treat_old_string_as_existing_content() -> None:
    assert (
        state_guard.open_note_reason(
            "Write",
            {
                "file_path": "/example/Open.md",
                "content": "- **new** x",
                "old_string": "- **new** x",
            },
        )
        is not None
    )


@pytest.mark.parametrize("suffix", ["None", "XXXX"])
def test_a_missing_note_path_cannot_match_a_configured_suffix(
    config_file: Path, suffix: str
) -> None:
    config_file.write_text(
        f'[harness]\nopen_note = "{suffix}"\ndecisions_dir = "/example/decisions"\n'
    )
    assert state_guard.open_note_reason("Write", {"content": "- **new** x"}) is None


@pytest.mark.parametrize("raw", [None, "invalid", []])
def test_invalid_tool_input_reaches_the_commit_check_as_empty_text(
    monkeypatch: pytest.MonkeyPatch, raw: object
) -> None:
    values: list[str] = []

    def command(text: str) -> None:
        values.append(text)

    monkeypatch.setattr(state_guard, "commit_trailer_reason", command)
    assert (
        state_guard.pre_tool_reason(
            {"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": raw}
        )
        is None
    )
    assert values == [""]


@pytest.mark.parametrize("flag", ["m", "F", "C", "c", "t", "U", "S", "u"])
def test_short_values_hide_the_rest_of_the_cluster(run: Runner, flag: str) -> None:
    assert run(bash(f"git commit -a{flag}fixes")) == (0, "", "")
    assert run(bash(f"git commit -as{flag}fixes")) == (2, "", TRAILER_MESSAGE)


@pytest.mark.parametrize("flag", sorted(state_guard.SHORT_VALUES))
def test_short_cluster_consumes_a_separate_value(run: Runner, flag: str) -> None:
    assert run(bash(f"git commit -a{flag} -s")) == (0, "", "")
    assert run(bash(f"git commit -a{flag} -s -s"))[0] == 2


def test_optional_short_values_do_not_consume_the_next_word(run: Runner) -> None:
    assert run(bash("git commit -S -s"))[0] == 2
    assert run(bash("git commit -u -s"))[0] == 2
    assert run(bash("git commit -m" + "fixes")) == (0, "", "")
    assert state_guard.short_signoff("-") == (False, False)


@pytest.mark.parametrize(
    "command",
    [
        "env -u NAME X=1 git commit -s",
        "env --unset NAME git commit -s",
        "env -i --unset=NAME git commit -s",
        "env -C path git commit -s",
        "env --chdir path git commit -s",
        "env -- git commit -s",
        "git --git-dir x commit -s",
        "git --work-tree x commit -s",
        "git --namespace x commit -s",
        "git --config-env x commit -s",
        'bash -c "git commit -s"',
        "sh -c 'git commit -s'",
        "zsh -c 'git commit -s'",
        "env -u NAME /bin/bash -c 'git commit -s'",
        'echo "$(git commit -s)"',
        'echo "`git commit -s`"',
        'echo "$(echo $(git commit -s))"',
    ],
)
def test_wrappers_options_and_quoted_scripts_are_checked(
    run: Runner, command: str
) -> None:
    assert run(bash(command)) == (2, "", TRAILER_MESSAGE)


@pytest.mark.parametrize(
    "command",
    [
        "env -u NAME",
        "env -i",
        "bash -c",
        "sh -x",
        "zsh -c 'git status'",
        "echo '$(git commit -s)'",
        "echo '`git commit -s`'",
        r'echo "\$(git commit -s)"',
        "bash -c 'echo \"git commit -s\"'",
    ],
)
def test_literal_text_inside_quotes_stays_allowed(run: Runner, command: str) -> None:
    assert run(bash(command)) == (0, "", "")


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ('$(echo "a)b")tail', ('echo "a)b"', 13)),
        ("$(echo 'a)b')tail", ("echo 'a)b'", 13)),
        (r"$(echo a\)b)tail", (r"echo a\)b", 12)),
        ("$(echo x", ("echo x", 8)),
        ("`echo x", ("echo x", 7)),
        ("`echo (x)`tail", ("echo (x)", 10)),
    ],
)
def test_quoted_substitution_boundaries(text: str, expected: tuple[str, int]) -> None:
    assert state_guard.quoted_substitution(text, 0) == expected


@pytest.mark.parametrize(
    "command",
    [
        "git commit -F message.txt",
        "git commit -F - <<'EOF'\nSigned-off-by: Example\nEOF",
        "git commit -F - <<EOF\nCo-authored-by: Example\nEOF",
    ],
)
def test_message_bodies_are_left_to_the_commit_message_check(
    run: Runner, command: str
) -> None:
    assert run(bash(command)) == (0, "", "")


def test_the_signoff_check_and_session_start_run_outside_a_repository(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, run: Runner
) -> None:
    monkeypatch.chdir(tmp_path)
    assert run(bash("git commit -s")) == (2, "", TRAILER_MESSAGE)
    assert run({"hook_event_name": "SessionStart"}) == (0, "", "")


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("bash -c", None),
        ('bash -x "git commit -s"', None),
        ('bash -x -c "git commit -s"', "git commit -s"),
    ],
)
def test_shell_script_requires_c_and_a_following_argument(
    command: str, expected: str | None
) -> None:
    assert state_guard.shell_script(command) == expected


@pytest.mark.parametrize(
    ("word", "expected"),
    [
        ("-s", (True, False)),
        ("message", (False, False)),
        ("--amend", (False, False)),
        ("-m", (False, True)),
        ("-m" + "fixes", (False, False)),
        ("-S", (False, False)),
    ],
)
def test_short_option_parser_reports_value_consumption(
    word: str, expected: tuple[bool, bool]
) -> None:
    assert state_guard.short_signoff(word) == expected


@pytest.mark.parametrize("script", ["echo X", "echo (x)", r"echo '\'", r"echo \)"])
def test_substitution_closes_after_literal_characters(script: str) -> None:
    text = f"$({script})tail"
    assert state_guard.quoted_substitution(text, 0) == (script, len(script) + 3)


def test_scanning_continues_after_a_quoted_substitution(run: Runner) -> None:
    assert run(bash('echo "$(git status)"; git commit -s')) == (2, "", TRAILER_MESSAGE)
    assert run(bash('echo "$(git status) $(git commit -s)"')) == (
        2,
        "",
        TRAILER_MESSAGE,
    )

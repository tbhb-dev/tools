# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns
"""Tests for tbhbdev alpha state-guard, against a config in a temporary XDG dir."""

import io
import json
import os
import runpy
import signal
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
lazy from pathlib import Path

import pytest

from tbhbdev.alpha import state_guard

CONFIG = """[harness]
rule = "Example rule: claims carry evidence."
decisions_dir = "/example/decisions"
open_note = "/example/Open.md"
"""
UNSUPPORTED_CLAIM = "The migration was applied to example-host."
TAG = "[state-guard: unverified]"
TRAILER_MESSAGE = (
    "state-guard: agent commits carry no Signed-off-by or Co-authored-by "
    "trailer and no -s or --signoff. Remove it.\n"
)

type Result = tuple[int, str, str]
type Runner = Callable[[object], Result]


@pytest.fixture(autouse=True)
def _private_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))


def assert_tagged(result: Result, message: str, **fields: object) -> None:
    status, out, err = result
    assert (status, err) == (0, "")
    assert json.loads(out) == {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "updatedInput": {"message": message, **fields},
        }
    }
    assert "permissionDecision" not in out


def test_flagged_messages_receive_a_guard_tag(run: Runner) -> None:
    assert_tagged(run(send(UNSUPPORTED_CLAIM)), f"{TAG} {UNSUPPORTED_CLAIM}")


@pytest.fixture(autouse=True)
def _bounded_hook() -> Iterator[None]:
    """A small hook payload must finish within its execution budget."""

    def expired(_number: int, _frame: object) -> None:
        message = "hook exceeded its execution budget"
        raise TimeoutError(message)

    previous = signal.signal(signal.Signals(14), expired)
    signal.alarm(1)
    try:
        yield
    finally:
        signal.alarm(0)
        signal.signal(signal.Signals(14), previous)


@pytest.fixture
def run(capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> Runner:
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


# ------------------------------------------------------------ config gating


@pytest.mark.parametrize("text", [None, "", "[other]\nkey = 1\n"])
def test_the_checks_that_need_no_private_value_run_without_config(
    config_file: Path, run: Runner, text: str | None
) -> None:
    if text is not None:
        config_file.write_text(text)
    assert_tagged(run(send(UNSUPPORTED_CLAIM)), f"{TAG} {UNSUPPORTED_CLAIM}")
    assert run(bash("git commit -s -m x")) == (2, "", TRAILER_MESSAGE)
    assert run(stop(last_assistant_message=UNSUPPORTED_CLAIM))[1] != ""


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
    assert run(send(UNSUPPORTED_CLAIM)) == (0, "", "")


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


# ------------------------------------------------------------------- claims


def test_tagging_can_be_disabled(config_file: Path, run: Runner) -> None:
    config_file.write_text("[state_guard]\ntag_messages = false\n")
    status, out, err = run(send(UNSUPPORTED_CLAIM))
    assert (status, out) == (2, "")
    assert err == (
        "state-guard: this message makes a status claim without evidence. Add "
        "the PR number and sha, the merge sha, command output or a log path, "
        "attribute it ('the reviewer says ...'), or say it is unverified. Offending: "
        f"{UNSUPPORTED_CLAIM}\n"
    )


def test_disabled_tagging_reports_claims_without_sender_tags(
    config_file: Path, tmp_path: Path, run: Runner
) -> None:
    config_file.write_text("[state_guard]\ntag_messages = false\n")
    status, out, err = run(send(f"It was {TAG} done."))
    assert (status, out) == (2, "")
    assert err.endswith("Offending: It was done.\n")
    assert not (tmp_path / "state").exists()


def test_at_most_three_offenders_are_listed_and_each_is_cut(
    config_file: Path, run: Runner
) -> None:
    config_file.write_text("[state_guard]\ntag_messages = false\n")
    lines = [f"Step {n} was done." for n in range(5)]
    lines[0] = "It was done " + "x" * 200
    _, _, err = run(send("\n".join(lines)))
    offending = err.split("Offending: ")[1].strip().split(" | ")
    assert offending == [
        ("It was done " + "x" * 200)[:160],
        "Step 1 was done.",
        "Step 2 was done.",
    ]


@pytest.mark.parametrize(
    "message",
    [
        "It was merged in a1b2c3d.",
        "It was merged in 0123456789abcdef0123456789abcdef01234567.",
        "PR #12 was merged.",
        "The reviewer says it was applied.",
        "The reviewer said it was applied.",
        "The host reports it was applied.",
        "It was applied; see run.log for details.",
        "It was applied, as `make check` shows.",
        "It was applied; the output is clean.",
        "It was applied, per the read-back.",
        "It was applied, which is unverified.",
        "It is not done.",
        "It is not applied yet.",
        "It was not merged.",
        "Nothing here claims anything.",
        "",
    ],
)
def test_evidence_attribution_and_unverified_pass(run: Runner, message: str) -> None:
    assert run(send(message)) == (0, "", "")


@pytest.mark.parametrize(
    "claim",
    [
        "It is done.",
        "They are installed.",
        "It was verified.",
        "It has been completed.",
        "It is now live.",
        "It shipped.",
        "We landed it.",
        "We rolled out the fix.",
        "It was IN PLACE.",
        "Item 12 was merged.",
        "It was applied to `x",
    ],
)
def test_each_kind_of_claim_is_blocked(run: Runner, claim: str) -> None:
    assert_tagged(run(send(claim)), f"{TAG} {claim}")
    assert json.loads(run(stop(last_assistant_message=claim))[1])["decision"] == "block"


def test_evidence_in_another_sentence_does_not_cover_a_claim(run: Runner) -> None:
    message = "It was applied. See #12 for the other one."
    assert_tagged(run(send(message)), f"{TAG} {message}")


def test_other_tools_and_odd_inputs_pass(run: Runner) -> None:
    for data in (
        {
            "hook_event_name": "PreToolUse",
            "tool_name": "Read",
            "tool_input": dict[str, object](),
        },
        {"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": None},
        {"hook_event_name": "PreToolUse", "tool_name": "SendMessage"},
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


# --------------------------------------------------------------------- stop


def test_stop_blocks_an_unsupported_claims_reply(run: Runner) -> None:
    status, out, err = run(stop(last_assistant_message=UNSUPPORTED_CLAIM))
    assert (status, err) == (0, "")
    decision = json.loads(out)
    assert decision["decision"] == "block"
    assert decision["reason"].startswith(
        "state-guard: your reply makes a status claim without evidence."
    )
    assert decision["reason"].endswith(f"Offending: {UNSUPPORTED_CLAIM}")


def test_stop_lets_an_evidenced_reply_and_a_stop_hook_loop_through(
    run: Runner,
) -> None:
    ok = stop(last_assistant_message="It was merged in a1b2c3d.")
    assert run(ok) == (0, "", "")
    again = stop(last_assistant_message=UNSUPPORTED_CLAIM, stop_hook_active=True)
    assert run(again) == (0, "", "")


def transcript(path: Path, entries: list[object]) -> str:
    path.write_text("\n".join(json.dumps(e) for e in entries) + "\nnot json\n")
    return str(path)


def reply(*blocks: object) -> dict[str, object]:
    return {"type": "assistant", "message": {"content": list(blocks)}}


def text_block(text: str) -> dict[str, str]:
    return {"type": "text", "text": text}


def test_stop_reads_the_last_reply_from_the_transcript(
    tmp_path: Path, run: Runner
) -> None:
    path = transcript(
        tmp_path / "t.jsonl",
        [
            {"type": "user", "message": {"content": "go"}},
            reply(text_block("Old claim: it was applied.")),
            {"type": "user", "message": {"content": "again"}},
            reply(
                {"type": "tool_use"},
                "stray",
                text_block("first"),
                text_block(UNSUPPORTED_CLAIM),
            ),
            {"type": "user", "message": {"content": [{"type": "tool_result"}]}},
            {"type": "system"},
            {"type": "assistant", "message": None},
            {"type": "assistant", "message": {"content": "str"}},
            "stray",
        ],
    )
    out = run(stop(transcript_path=path))[1]
    assert json.loads(out)["reason"].endswith(f"Offending: {UNSUPPORTED_CLAIM}")


def test_stop_resets_at_a_new_prompt_and_tolerates_a_missing_transcript(
    tmp_path: Path, run: Runner
) -> None:
    path = transcript(
        tmp_path / "t.jsonl",
        [
            reply(text_block(UNSUPPORTED_CLAIM)),
            {"type": "user", "message": {"content": "x"}},
        ],
    )
    assert run(stop(transcript_path=path)) == (0, "", "")
    assert run(stop(transcript_path=str(tmp_path / "none.jsonl"))) == (0, "", "")
    assert run(stop()) == (0, "", "")


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


@pytest.mark.parametrize("tool", ["Bash", "SendMessage"])
@pytest.mark.parametrize("raw", [None, "invalid", []])
def test_invalid_tool_input_reaches_checks_as_empty_text(
    monkeypatch: pytest.MonkeyPatch, tool: str, raw: object
) -> None:
    values: list[str] = []

    def command(text: str) -> None:
        values.append(text)

    def message(text: str) -> list[str]:
        values.append(text)
        return []

    monkeypatch.setattr(state_guard, "commit_trailer_reason", command)
    monkeypatch.setattr(state_guard, "unsupported_claims", message)
    assert (
        state_guard.pre_tool_reason(
            {"hook_event_name": "PreToolUse", "tool_name": tool, "tool_input": raw}
        )
        is None
    )
    assert values == [""]


def test_empty_and_missing_transcripts_have_no_reply(tmp_path: Path) -> None:
    empty = tmp_path / "empty.jsonl"
    empty.write_text("")
    assert state_guard.last_assistant_text(str(empty)) == ""
    assert state_guard.last_assistant_text(str(tmp_path / "missing.jsonl")) == ""


def test_a_malformed_transcript_line_does_not_hide_the_next_reply(
    tmp_path: Path,
) -> None:
    path = tmp_path / "reply.jsonl"
    path.write_text(
        'not json\n{"type":"assistant","message":{"content":'
        '[{"type":"text","text":"reply"}]}}\n'
    )
    assert state_guard.last_assistant_text(str(path)) == "reply"


def test_a_new_prompt_or_empty_text_block_clears_the_previous_reply() -> None:
    assert (
        state_guard.entry_text(
            {"type": "user", "message": {"content": "new prompt"}}, "old reply"
        )
        == ""
    )
    assert (
        state_guard.entry_text(
            {"type": "assistant", "message": {"content": [{"type": "text"}]}},
            "old reply",
        )
        == ""
    )


def test_a_missing_transcript_path_does_not_read_a_default_filename(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    for name in ("None", "XXXX"):
        (tmp_path / name).write_text(
            json.dumps(
                {
                    "type": "assistant",
                    "message": {
                        "content": [{"type": "text", "text": UNSUPPORTED_CLAIM}]
                    },
                }
            )
            + "\n"
        )
    assert state_guard.stop_reason({}) is None


def test_transcript_text_uses_utf8_even_when_the_locale_does_not(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "unicode.jsonl"
    path.write_text(
        '{"type":"assistant","message":{"content":'
        '[{"type":"text","text":"caf\u00e9"}]}}\n',
        encoding="utf-8",
    )

    def open_ascii(filename: str, *, encoding: str | None = None) -> object:
        return Path(filename).open(encoding=encoding or "ascii")

    monkeypatch.setattr(state_guard, "open", open_ascii, raising=False)
    assert state_guard.last_assistant_text(str(path)) == "caf\u00e9"


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
    "message",
    ["I landed on this design.", "We LANDED ON a design.", "I landed\ton this design."],
)
def test_landed_on_is_not_a_status_claim(run: Runner, message: str) -> None:
    assert run(send(message)) == (0, "", "")


@pytest.mark.parametrize(
    "candidate",
    [
        "a" * 7,
        "deadbeef",
        "a12345",
        "a" + "1" * 40,
        "ga123456",
        "a123456g",
    ],
)
def test_evidence_requires_a_bounded_mixed_hex_hash(
    run: Runner, candidate: str
) -> None:
    message = f"It was merged in {candidate}."
    assert_tagged(run(send(message)), f"{TAG} {message}")


@pytest.mark.parametrize(
    "candidate", ["a123456", "123456a", "A123456", "123456A", "a" + "1" * 39]
)
def test_hash_evidence_accepts_both_cases_and_length_limits(
    run: Runner, candidate: str
) -> None:
    assert run(send(f"It was merged in {candidate}.")) == (0, "", "")


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


def test_claim_and_signoff_checks_run_outside_a_repository_without_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, run: Runner
) -> None:
    monkeypatch.chdir(tmp_path)
    assert_tagged(run(send(UNSUPPORTED_CLAIM)), f"{TAG} {UNSUPPORTED_CLAIM}")
    assert run(bash("git commit -s")) == (2, "", TRAILER_MESSAGE)
    assert run(stop(last_assistant_message=UNSUPPORTED_CLAIM))[1] != ""
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


@pytest.mark.parametrize(
    ("passing", "blocking"),
    [
        ("The fix was not merged.", "It was not slow, and the fix is merged."),
        ("The fix isn't deployed.", "It isn't slow; the fix is deployed."),
        ("Nothing merged yet.", "Nothing changed, but the fix merged."),
        ("Neither change was merged.", "Neither failed, and the fix merged."),
        ("None were deployed.", "None failed; the fix deployed."),
        (
            "The fix is no longer deployed.",
            "It is no longer slow, and the fix deployed.",
        ),
        ("The fix never merged.", "It never failed, but the fix merged."),
        ("The fix has not merged yet.", "It has not failed yet; the fix merged."),
        ("I'll have the fix merged.", "I'll check later; the fix merged."),
        ("I will get the fix deployed.", "I will check later, but the fix deployed."),
        ("The fix will be merged.", "The fix merged; the build will follow."),
        ("The fix would be merged.", "It would help, but the fix merged."),
        ("The fix should be merged.", "It should help; the fix merged."),
        (
            "Once the fix is merged, check again.",
            "Once ready, check again; the fix merged.",
        ),
        (
            "When the fix is merged, check again.",
            "When ready, check again; the fix merged.",
        ),
        (
            "If the fix is merged, check again.",
            "If ready, check again; the fix merged.",
        ),
        ("After the fix is merged, check again.", "After lunch, the fix merged."),
        ("Before the fix is merged, check again.", "Before lunch, the fix merged."),
        ("Until the fix is merged, wait.", "Until tomorrow, wait; the fix merged."),
        ("Unless the fix is merged, wait.", "Unless ready, wait; the fix merged."),
        (
            "Whether the fix is merged matters.",
            "Whether ready or late, the fix merged.",
        ),
        (
            "As soon as the fix is merged, check.",
            "As soon as ready, check; the fix merged.",
        ),
        ("Is the fix merged?", "The fix is merged."),
        ("Please get the fix merged.", "Please check later; the fix merged."),
        ("Could you get the fix merged?", "Could you check? The fix merged."),
        ("Can you confirm the fix is merged?", "Can you check? The fix is merged."),
        ("Tell me whether the fix is merged.", "Tell me later; the fix merged."),
        ("Let me know once the fix is merged.", "Let me know later; the fix merged."),
        (
            "Ask the reviewer to get the fix merged.",
            "Ask the reviewer later; the fix merged.",
        ),
        ("The fix merged, per reviewer.", "The fix merged, perhaps."),
        ("The fix merged; Source: review.", "The fix merged; resources pending."),
        ("The fix merged; I read back the result.", "The fix merged; I read the plan."),
        ("The fix merged; I read them back.", "The fix merged; I read them."),
        ("The fix merged in run 12345.", "The fix merged in 1234."),
        ('The fix merged: "merge completed".', 'The fix merged: "ok".'),
        ("The fix merged; see notes/check.txt.", "The fix merged; see check.txt."),
        ("The fix merged; see Decisions/check.", "The fix merged; see notes/check."),
        ("The merged PRs' worktrees need attention.", "The PR is merged."),
        ("Two applied rulesets need attention.", "Both PRs merged."),
        ("Our deployed changes need attention.", "Our changes deployed."),
        ("3 merged changes need attention.", "3 changes merged."),
    ],
)
def test_claim_scope_and_evidence(passing: str, blocking: str) -> None:
    assert state_guard.unsupported_claims(passing) == []
    assert state_guard.unsupported_claims(blocking) == [
        blocking.rsplit("? ", maxsplit=1)[-1]
    ]


@pytest.mark.parametrize(
    "text",
    [
        "The fix merged yesterday.",
        "Last time the fix merged, we waited.",
        "Earlier the fix merged.",
        "The fix merged before your message.",
        "The fix merged at 09:30 before lunch.",
        'The "own wording" is fixed.',
        'I chose "the new setting" and applied.',
        "The build is complete: there are 12345 tasks.",
        "The build is running in notes/check.txt.",
        "The build is running (Decisions/check).",
        "The fix has landed, and I have not checked it per finding.",
        "The fix is not only merged.",
        "The fix is not just merged.",
    ],
)
def test_incidental_context_still_blocks(text: str) -> None:
    assert state_guard.unsupported_claims(text) == [text]


@pytest.mark.parametrize(
    "text",
    [
        'The command printed "the fix merged".',
        "The fix merged in build 12345.",
        "The fix merged; SEE notes/check.txt.",
        "Is the fix merged?   ",
        "No build is running.",
        "I applied nothing.",
        "I approved, merged and pushed nothing.",
        "I haven't approved, merged or pushed anything.",
        "The fix would be queued and merged.",
        "The merged-code reference needs review.",
        "The config/deployed.txt reference needs review.",
        "Both merged changes need review.",
        "The reviewer's merged changes need review.",
        "The reviewers' merged changes need review.",
    ],
)
def test_additional_scoped_exceptions_and_evidence(text: str) -> None:
    assert state_guard.unsupported_claims(text) == []


@pytest.mark.parametrize(
    "text",
    [
        "The merged code is deployed.",
        "The fix merged; see notes/check.",
        "The fix merged; see later.",
        'The fix "merged without a closing quote.',
        "No deadline yet (the build is running).",
        'The fix is done, but the plan would mention "future work".',
        "The build is RUNNING; see notes/check.txt.",
        "The fix is merged, not the build.",
        'The "label" is merged, "',
    ],
)
def test_evidence_boundaries_still_block(text: str) -> None:
    assert state_guard.unsupported_claims(text) == [text]


@pytest.mark.parametrize("ids", [False, True])
def test_corpus_measurement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    ids: bool,
) -> None:
    rows = [
        (1, "A", "bare_claim", "The build is done."),
        (2, "A", "negation", "No build is running."),
        (3, "B", "evidence_missed", "PR #12 merged."),
        (4, "B", "evidence_missed", "The fix merged."),
        (5, "C", "future", "The fix should be merged."),
        (6, "C", "other", "The fix deployed."),
    ]
    (tmp_path / "window.jsonl").write_text(
        "\n".join(
            json.dumps({"id": n, "kind": "stop", "off": text}) for n, _, _, text in rows
        )
    )
    (tmp_path / "labels.jsonl").write_text(
        "\n".join(
            json.dumps(
                {"id": n, "verdict": verdict, "shape": shape, "note": "synthetic"}
            )
            for n, verdict, shape, _ in rows
        )
    )
    (tmp_path / "anchors.json").write_text(
        json.dumps(
            [
                {"n": 0, "verdict": "A", "ids": [1]},
                {"n": 1, "verdict": "A", "ids": [1, 2]},
                {"n": 2, "verdict": "B", "ids": [3]},
            ]
        )
    )
    script = Path(__file__).resolve().parents[1] / "state_guard_corpus.py"
    monkeypatch.setattr(
        "sys.argv", [str(script), str(tmp_path), *(["--ids"] if ids else [])]
    )
    runpy.run_path(str(script), run_name="__main__")
    expected = (
        "verdict A: 1/2\nverdict B: 1/2\nverdict C: 1/2\n"
        "shape bare_claim: 1/1\nshape evidence_missed: 1/2\n"
        "shape future: 0/1\nshape negation: 0/1\nshape other: 1/1\n"
        "A anchors: 1/2\n"
    )
    if ids:
        expected += "A pass ids: [2]\nB block ids: [4]\nC block ids: [6]\n"
    assert capsys.readouterr().out == expected


@pytest.mark.parametrize(
    "text",
    [
        "Both deployed cleanly.",
        "Both merged successfully.",
        "3 merged today.",
        "That landed cleanly.",
        "It's merged upstream.",
        "It's deployed everywhere.",
        "That's applied now.",
        "After the review the PR is merged.",
        "Please note the PR is merged.",
        "There is no doubt the PR is merged.",
        "Since nothing failed the deploy is done.",
        "Once again the deploy is done.",
        "Batch 2 is running at 3 per cent CPU.",
        "Build 12345 failed, and the deploy is done.",
        'The PR is merged, "trust me".',
        "The PR is merged in tbhb-dev/tools.git",
        "The deploy is live, 20241111 users.",
        "The PR is merged.",
        "Both PRs merged.",
    ],
)
def test_verified_status_bypasses_block(text: str) -> None:
    assert state_guard.unsupported_claims(text) == [text]


@pytest.mark.parametrize(
    "text",
    [
        "Two deployed yesterday.",
        "One merged already.",
        "Both applied.",
        "7 merged overnight.",
        "She's merged upstream.",
        "He's deployed today.",
        "Before lunch the patch is merged.",
        "When convenient remember the deploy is done.",
        "Please remember the build is complete.",
        "No question the deploy is live.",
        "Since none failed the patch is merged.",
        "The worker is running at 2 per host.",
        "Job 54321 failed; the patch is merged.",
        'The deploy is done, "all good".',
        "The patch is merged, 987654 users waiting.",
    ],
)
def test_more_incidental_status_context_blocks(text: str) -> None:
    assert state_guard.unsupported_claims(text) == [text]


@pytest.mark.parametrize(
    "text",
    [
        "The fix merged in 12345.",
        'The fix merged: "yes".',
        'The build is RUNNING: "started".',
    ],
)
def test_legacy_unsubstantiated_read_backs_block(text: str) -> None:
    assert state_guard.unsupported_claims(text) == [text]


@pytest.mark.parametrize(
    "text",
    [
        "When it's merged, check again.",
        "If it merged, check again.",
        "None of the changes has merged.",
        "None of those has merged.",
        "Neither PR has merged.",
        "The fix merged per Alice.",
        "The fix merged per maintainer.",
        "The fix merged per operator.",
        "The fix merged per notes/check.txt.",
        "The fix merged per my read.",
        "The fix merged per my grep.",
        "The fix merged per my check.",
        'The fix merged, "notes/check.txt".',
        'The fix merged: "123456789012".',
        'The fix merged: "status ready".',
        'The command reads "the fix merged".',
        'The command shows "the fix merged".',
        'The command returned "the fix merged".',
        'The command PRINTED "the fix merged".',
        'The "label" is merged: "merge completed".',
        "The fix merged in notes/check.txt.",
        "The fix merged (Decisions/check).",
        "The fix merged in check run 12345.",
        "The fix merged in task 12345.",
        "The fix merged in pid 12345.",
        "The fix merged in job 12345.",
        "The fix merged in id 12345.",
        "The fix merged in commit 12345.",
        "The fix merged in review 12345.",
        "The fix merged in event 12345.",
        "Ask " + "a" * 78 + " to get it merged.",
    ],
)
def test_scoped_qualifiers_and_attached_evidence_pass(text: str) -> None:
    assert state_guard.unsupported_claims(text) == []


@pytest.mark.parametrize(
    "text",
    [
        "Build 12345 failed, the deploy is done.",
        "The deploy is done, build 12345 failed.",
        'The fix merged: "merge ready".',
        'The fix merged: "123".',
        'The fix merged, "status ready".',
        'The fix merged, "trust me completely".',
        'The "12345" label is merged.',
        'The "notes/check.txt" label is merged.',
        "The worker is running at 3 per Cent.",
        "The worker is running at 3 per percent.",
        "The worker is running at 3 per day.",
        "The worker is running at 3 per Hour.",
        "The worker is running at 3 per minute.",
        "The worker is running at 3 per second.",
        "The worker is running at 3 per week.",
        "The worker is running at 3 per month.",
        "The worker is running at 3 per run.",
        "The worker is running at 3 per Host.",
        "There's merged upstream.",
        "What's merged today.",
        "Who's deployed everywhere.",
        "Here's applied now.",
        "Ask " + "a" * 79 + " to get it merged.",
        "Ask later. Remember to say the fix merged.",
        "Ask later; remember to say the fix merged.",
    ],
)
def test_evidence_and_qualifier_limits_block(text: str) -> None:
    assert state_guard.unsupported_claims(text) == [text.rsplit(". ", maxsplit=1)[-1]]


@pytest.mark.parametrize(
    "adverb",
    [
        "well",
        "elsewhere",
        "tonight",
        "soon",
        "later",
        "earlier",
        "again",
        "together",
        "apart",
        "here",
        "there",
        "abroad",
        "online",
        "offline",
        "away",
        "back",
        "ahead",
    ],
)
def test_adverbs_after_quantified_statuses_block(adverb: str) -> None:
    text = f"Both deployed {adverb}."
    assert state_guard.unsupported_claims(text) == [text]


@pytest.mark.parametrize(
    "text",
    [
        "The fix is merged, right?",
        "I will report that the fix is merged.",
        "This should be fine since the fix is merged.",
        "4 merged out of 12.",
        "5 merged so far.",
        "Three merged this morning.",
        'The fix is merged ("1").',
        "The fix is merged per Friday.",
    ],
)
def test_review_findings_block(text: str) -> None:
    assert state_guard.unsupported_claims(text) == [text]


@pytest.mark.parametrize(
    "text",
    [
        "Is the fix merged?",
        "Has it deployed?",
        "Which PR merged?",
        "It will be merged after review.",
        "It should have deployed by tomorrow.",
        "I'll get it merged.",
        "4 merged PRs remain open.",
        'The fix is merged ("abc1234").',
        "The fix is merged per the deploy log.",
    ],
)
def test_review_legitimate_shapes_pass(text: str) -> None:
    assert state_guard.unsupported_claims(text) == []


@pytest.mark.parametrize(
    "text",
    [
        "The patches are deployed, aren't they?",
        "The change was merged, wasn't it?",
        "The patch is applied, OK?",
        "I would announce that the patch merged.",
        "It should be easy because the patch deployed.",
        "I will wait, the patch is merged.",
        "Eleven merged out of twenty.",
        "Several deployed so far.",
        "A few merged this evening.",
        "All deployed last Monday.",
    ],
)
def test_additional_review_claims_block(text: str) -> None:
    assert state_guard.unsupported_claims(text) == [text]


@pytest.mark.parametrize(
    "tag",
    [
        "right",
        "correct",
        "yes",
        "no",
        "isn't it",
        "aren't they",
        "wasn't it",
        "OK",
        "yeah",
    ],
)
def test_tag_questions_still_assert_status(tag: str) -> None:
    text = f"The fix is merged, {tag}?"
    assert state_guard.unsupported_claims(text) == [text]


@pytest.mark.parametrize(
    "connector", ["that", "since", "because", "as", "so", "which", ","]
)
def test_modals_do_not_govern_complement_claims(connector: str) -> None:
    text = f"I will announce {connector} the fix is merged."
    assert state_guard.unsupported_claims(text) == [text]


@pytest.mark.parametrize(
    "quantity",
    [
        "one",
        "two",
        "three",
        "four",
        "five",
        "six",
        "seven",
        "eight",
        "nine",
        "ten",
        "eleven",
        "twelve",
        "thirteen",
        "fourteen",
        "fifteen",
        "sixteen",
        "seventeen",
        "eighteen",
        "nineteen",
        "twenty",
        "both",
        "all",
        "several",
        "a few",
        "4",
    ],
)
@pytest.mark.parametrize(
    "suffix", ["out of 30", "so far", "this morning", "", "quickly"]
)
def test_counted_participles_need_a_noun(quantity: str, suffix: str) -> None:
    text = f"{quantity} merged {suffix}."
    assert state_guard.unsupported_claims(text) == [text]
    assert state_guard.unsupported_claims(f"{quantity} merged PRs remain open.") == []


@pytest.mark.parametrize(
    "time",
    [
        "Monday",
        "Tuesday",
        "Wednesday",
        "Thursday",
        "Friday",
        "Saturday",
        "Sunday",
        "January",
        "February",
        "March",
        "April",
        "May",
        "June",
        "July",
        "August",
        "September",
        "October",
        "November",
        "December",
        "today",
        "tonight",
        "yesterday",
        "tomorrow",
        "now",
        "morning",
        "evening",
        "schedule",
    ],
)
def test_time_is_not_attribution(time: str) -> None:
    text = f"The fix is merged per {time}."
    assert state_guard.unsupported_claims(text) == [text]


@pytest.mark.parametrize(
    "text",
    [
        "It will soon be merged.",
        "It would probably have been merged.",
        "It should not get merged.",
        "I'll have the fix merged.",
        'The fix merged ("ABC1234").',
        'The fix merged ("#123").',
    ],
)
def test_direct_modal_phrases_and_quoted_artifacts_pass(text: str) -> None:
    assert state_guard.unsupported_claims(text) == []


@pytest.mark.parametrize("content", ["1", "123", "123456", "job 1234", "a1b2c3"])
def test_short_quoted_numbers_are_not_artifacts(content: str) -> None:
    text = f'The fix merged ("{content}").'
    assert state_guard.unsupported_claims(text) == [text]


@pytest.mark.parametrize(
    "text",
    [
        "Both merged hours ago.",
        "4 merged without errors.",
        'The fix merged, "job 12345 failed".',
        'The fix merged, "job 12345".',
        'The build is running ("notes/check.txt").',
        'The build is started ("notes/check.txt").',
        'The build is RUNNING: "worker started".',
        'The fix merged, "Decisions/check".',
        'The fix merged job"aside" 12345.',
        'The fix merged in notes/"aside"check.txt.',
        "The worker is running at 20 per CPU.",
        "The worker is running at 20 per GPU.",
        "The worker is running at 20 per MiB.",
        "The worker is running at 20 per 4.",
        "Will just merged the fix.",
        "Will merged the fix.",
        "Will quickly merged the fix.",
        "What I changed is deployed; can you check?",
        "Did you see it? The fix merged.",
    ],
)
def test_reviewed_status_bypasses_block(text: str) -> None:
    assert state_guard.unsupported_claims(text)


@pytest.mark.parametrize(
    "content", ["defaced", "aaaaaaa", "1234567", "a" * 40, "1" * 40]
)
def test_quoted_hash_needs_a_digit_and_a_hex_letter(content: str) -> None:
    text = f'The fix merged ("{content}").'
    assert state_guard.unsupported_claims(text) == [text]


@pytest.mark.parametrize(
    "text",
    [
        "Both merged changes need review.",
        "4 merged fixes need review.",
        'The fix merged, "notes/check.txt".',
        'The fix merged, "abc1234".',
        'The fix merged, "#123".',
        'The fix merged "aside" in job 12345.',
        'The fix merged "aside" in notes/check.txt.',
        'The fix merged; see "aside" notes/check.txt.',
        "The worker is running per notes/check.txt.",
        "The worker is running per CPU/check.txt.",
        "The worker is running per GPU/check.txt.",
        "The worker is running per MiB/check.txt.",
        "The worker is running per the worker log.",
        "The worker is running per Alice.",
        "It will just be merged.",
        "It will just merged.",
        "Will just be merged.",
        "Did you see that the fix merged?",
        "What changed is deployed?",
    ],
)
def test_reviewed_exemptions_stay_scoped(text: str) -> None:
    assert state_guard.unsupported_claims(text) == []


@pytest.mark.parametrize(
    "unit", ["second", "minute", "hour", "day", "week", "month", "year"]
)
@pytest.mark.parametrize("ending", ["", "s"])
def test_counted_participles_with_elapsed_time_block(unit: str, ending: str) -> None:
    text = f"Both merged {unit}{ending} ago."
    assert state_guard.unsupported_claims(text) == [text]


@pytest.mark.parametrize("content", ["job 12345", "Decisions/check"])
def test_unmarked_quotes_do_not_supply_artifacts(content: str) -> None:
    text = f'The fix merged "{content}".'
    assert state_guard.unsupported_claims(text) == [text]


def test_tagged_input_copies_all_fields_and_each_flagged_occurrence(
    run: Runner,
) -> None:
    message = "A note. It was done. It was merged in #12. It was done. It shipped."
    fields = {"to": "peer-example", "summary": "note", "extra": {"n": 3}, "flag": False}
    data = send(message)
    data["tool_input"] = {"message": message, **fields}
    assert_tagged(
        run(data),
        f"A note. {TAG} It was done. It was merged in #12. "
        f"{TAG} It was done. {TAG} It shipped.",
        **fields,
    )
    assert data["tool_input"] == {"message": message, **fields}


@pytest.mark.parametrize(
    "prefix",
    [
        "- ",
        "  + ",
        "* ",
        "1. ",
        "2) ",
        "**Status:** ",
        "**Status**: ",
        "  - **Status:** ",
        "  - **Status**: ",
        "\t",
    ],
)
def test_tags_follow_list_markers_and_bold_labels(run: Runner, prefix: str) -> None:
    assert_tagged(
        run(send(f"{prefix}It was done.\n{prefix}A note.")),
        f"{prefix}{TAG} It was done.\n{prefix}A note.",
    )


@pytest.mark.parametrize("tag", [TAG, TAG + " ", TAG + " " + TAG + " "])
def test_sender_tags_cannot_preempt_the_guard(run: Runner, tag: str) -> None:
    assert_tagged(run(send(f"{tag}It was done.")), f"{TAG} {tag}It was done.")
    assert_tagged(
        run(send(f"{tag}It was done. {tag}A note.")),
        f"{TAG} {tag}It was done. {tag}A note.",
    )
    assert run(send(f"{tag}A note.")) == (0, "", "")
    assert_tagged(run(send(f"It was {tag}done.")), f"{TAG} It was {tag}done.")


@pytest.mark.parametrize("tag", [TAG, TAG + " ", TAG + " " + TAG + " "])
def test_stop_rejects_copied_tags(run: Runner, tag: str) -> None:
    status, out, err = run(stop(last_assistant_message=f"{tag}It was done."))
    assert (status, err) == (0, "")
    assert json.loads(out) == {
        "decision": "block",
        "reason": state_guard.claim_reason("your reply", ["It was done."]),
    }


def test_tagged_sentences_are_logged_privately(tmp_path: Path, run: Runner) -> None:
    data = send("It was done. A note. It shipped.")
    data["session_id"] = "session-example"
    data["tool_input"] = {
        "message": "It was done. A note. It shipped.",
        "to": "peer-example",
    }
    before = datetime.now(UTC)
    assert_tagged(
        run(data), f"{TAG} It was done. A note. {TAG} It shipped.", to="peer-example"
    )
    path = tmp_path / "state/tbhbdev/state-guard/tagged.jsonl"
    records = [json.loads(line) for line in path.read_text().splitlines()]
    assert len(records) == 2
    for record, rule, sentence in zip(
        records, ["was done", "shipped"], ["It was done.", "It shipped."], strict=True
    ):
        time = datetime.fromisoformat(record.pop("time"))
        assert time.utcoffset() == UTC.utcoffset(time)
        assert before <= time <= datetime.now(UTC)
        assert record == {
            "session_id": "session-example",
            "rule": rule,
            "sentence": sentence,
            "recipient": "peer-example",
        }
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700
    assert_tagged(run(send("It was done.")), f"{TAG} It was done.")
    records = [json.loads(line) for line in path.read_text().splitlines()]
    assert len(records) == 3
    assert records[-1]["session_id"] is None
    assert "recipient" not in records[-1]


def test_logging_failure_still_sends_once(tmp_path: Path, run: Runner) -> None:
    path = tmp_path / "state/tbhbdev/state-guard"
    path.parent.mkdir(parents=True)
    path.write_text("a file blocks the directory")
    status, out, err = run(send("It was done. It shipped."))
    assert len(err.splitlines()) == 1
    assert err.startswith("state-guard: could not log tagged sentences:")
    assert_tagged((status, out, ""), f"{TAG} It was done. {TAG} It shipped.")


def test_long_sentences_preserve_full_text_and_evidence(run: Runner) -> None:
    sentence = "It was done " + "x" * 200
    message = f"{sentence}. {sentence}. {sentence} in #12."
    assert_tagged(
        run(send(message)), f"{TAG} {sentence}. {TAG} {sentence}. {sentence} in #12."
    )


@pytest.mark.parametrize("setting", ["tag_messages = true", 'other = "value"'])
def test_an_existing_table_defaults_to_tagging(
    config_file: Path, run: Runner, setting: str
) -> None:
    config_file.write_text(f"[state_guard]\n{setting}\n")
    assert_tagged(run(send("It was done.")), f"{TAG} It was done.")


def test_logging_tightens_existing_permissions(tmp_path: Path, run: Runner) -> None:
    path = tmp_path / "state/tbhbdev/state-guard/tagged.jsonl"
    path.parent.mkdir(parents=True)
    path.parent.chmod(0o755)
    path.write_text("existing\n")
    path.chmod(0o644)
    assert_tagged(run(send("It was done.")), f"{TAG} It was done.")
    lines = path.read_text().splitlines()
    assert lines[0] == "existing"
    assert json.loads(lines[1])["sentence"] == "It was done."
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700


def test_logs_use_the_home_state_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, run: Runner
) -> None:
    monkeypatch.delenv("XDG_STATE_HOME")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert_tagged(run(send("It was done.")), f"{TAG} It was done.")
    path = tmp_path / ".local/state/tbhbdev/state-guard/tagged.jsonl"
    assert json.loads(path.read_text())["rule"] == "was done"


def test_log_is_private_at_creation(
    monkeypatch: pytest.MonkeyPatch, run: Runner
) -> None:
    original = os.open
    modes: list[int] = []

    def observe(path: Path, flags: int, mode: int = 0o777) -> int:
        fd = original(path, flags, mode)
        modes.append(os.fstat(fd).st_mode & 0o777)
        return fd

    monkeypatch.setattr(os, "open", observe)
    previous = os.umask(0)
    try:
        assert_tagged(run(send("It was done.")), f"{TAG} It was done.")
    finally:
        os.umask(previous)
    assert modes == [0o600]


def test_log_failures_with_multiple_lines_print_one_stderr_line(
    monkeypatch: pytest.MonkeyPatch, run: Runner
) -> None:
    def broken(_path: Path) -> Path:
        message = "one\ntwo"
        raise RuntimeError(message)

    monkeypatch.setattr(state_guard, "private_dir", broken)
    status, out, err = run(send("It was done."))
    assert (
        err
        == "state-guard: could not log tagged sentences: RuntimeError('one\\ntwo')\n"
    )
    assert_tagged((status, out, ""), f"{TAG} It was done.")


@pytest.mark.parametrize("tool", ["Read", "Write", "Edit", "Bash", "Other"])
def test_tags_never_touch_other_tools(run: Runner, tool: str) -> None:
    data = send(UNSUPPORTED_CLAIM)
    data["tool_name"] = tool
    assert run(data) == (0, "", "")
    assert data["tool_input"] == {"message": UNSUPPORTED_CLAIM}


@pytest.mark.parametrize("event", ["PostToolUse", "SessionStart", "Other", None])
def test_tags_only_apply_on_pre_tool_use(run: Runner, event: str | None) -> None:
    data = send(UNSUPPORTED_CLAIM)
    data["hook_event_name"] = event
    assert run(data) == (0, "", "")

# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns
"""Tests for tbhbdev alpha state-guard, against a config in a temporary XDG dir."""

import io
import json
import os
import runpy
import signal
import subprocess
import sys
import unicodedata
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from typing import override
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


@pytest.fixture(autouse=True)
def _tagging_on(config_file: Path) -> None:
    config_file.write_text("[state_guard]\ntag_messages = true\n")


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


# ------------------------------------------------------------ config gating


@pytest.mark.parametrize("text", [None, "", "[other]\nkey = 1\n"])
def test_the_checks_that_need_no_private_value_run_without_config(
    config_file: Path, run: Runner, text: str | None
) -> None:
    config_file.unlink()
    if text is not None:
        config_file.write_text(text)
    assert run(send(UNSUPPORTED_CLAIM))[0] == 2
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
    assert run(bash("git status")) == (0, "", "")


@pytest.mark.parametrize(
    "stage",
    [
        "tagging_enabled",
        "tag_sentences",
    ],
)
def test_a_crash_on_the_tagging_path_keeps_the_refusal(
    run: Runner, monkeypatch: pytest.MonkeyPatch, stage: str
) -> None:
    def boom(*_args: object) -> object:
        message = f"{stage} broke"
        raise RuntimeError(message)

    monkeypatch.setattr(state_guard, stage, boom)
    assert refused(run(send(UNSUPPORTED_CLAIM)))


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
        "state-guard: this message makes a status claim without evidence, so it "
        f"was not sent. Offending: {UNSUPPORTED_CLAIM}. Resend the same message "
        "with only these sentences fixed (add the PR number and sha, the merge "
        "sha, command output or a log path, attribute it, or say it is "
        "unverified); leave the rest unchanged.\n"
    )


def test_disabled_tagging_reports_claims_without_sender_tags(
    config_file: Path, tmp_path: Path, run: Runner
) -> None:
    config_file.write_text("[state_guard]\ntag_messages = false\n")
    status, out, err = run(send(f"It was {TAG} done."))
    assert (status, out) == (2, "")
    assert "Offending: It was done.. Resend the same message" in err
    assert not (tmp_path / "state").exists()


@pytest.mark.parametrize("final", [False, True])
def test_at_most_three_offenders_are_listed_and_each_is_cut(
    config_file: Path, run: Runner, final: bool
) -> None:
    config_file.write_text("[state_guard]\ntag_messages = false\n")
    lines = [f"Step {n} was done." for n in range(5)]
    lines[0] = "It was done " + "x" * 200
    message = "\n".join(lines)
    if final:
        _, out, _ = run(stop(last_assistant_message=message))
        reason = json.loads(out)["reason"]
        suffix = ". Fix only these sentences"
    else:
        _, _, reason = run(send(message))
        suffix = ". Resend the same message"
    offending = reason.split("Offending: ")[1].split(suffix)[0]
    offending = offending.split(" | ")
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
    assert decision["reason"] == (
        "state-guard: your reply makes a status claim without evidence. "
        f"Offending: {UNSUPPORTED_CLAIM}. Fix only these sentences (add the PR "
        "number and sha, the merge sha, command output or a log path, attribute "
        "it, or say it is unverified) and send just the corrected sentences; "
        "don't repeat the rest of the reply."
    )


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
    assert (
        f"Offending: {UNSUPPORTED_CLAIM}. Fix only these sentences"
        in json.loads(out)["reason"]
    )


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
    assert set(values) == {""}  # SendMessage checks once per rendering


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
        (5, "C", "future", "Tell me when it is merged."),
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
        "verdict A: 2/2\nverdict B: 1/2\nverdict C: 2/2\n"
        "shape bare_claim: 1/1\nshape evidence_missed: 1/2\n"
        "shape future: 1/1\nshape negation: 1/1\nshape other: 1/1\n"
        "A anchors: 2/2\n"
    )
    if ids:
        expected += "A pass ids: []\nB block ids: [4]\nC block ids: [5, 6]\n"
    assert capsys.readouterr().out == expected


@pytest.mark.parametrize(
    "text",
    [
        "Nothing merged yet.",
        "Once the fix is merged, check again.",
        "When the fix is merged, check again.",
        "If the fix is merged, check again.",
        "After the fix is merged, check again.",
        "Before the fix is merged, check again.",
        "Until the fix is merged, wait.",
        "Unless the fix is merged, wait.",
        "Whether the fix is merged matters.",
        "As soon as the fix is merged, check.",
    ],
)
def test_claim_scope_and_evidence(text: str) -> None:
    assert state_guard.unsupported_claims(text) == [text]


@pytest.mark.parametrize(
    "text",
    [
        "I approved, merged and pushed nothing.",
        "I haven't approved, merged or pushed anything.",
        "The fix would be queued and merged.",
        "The merged-code reference needs review.",
        "The config/deployed.txt reference needs review.",
        "The reviewer's merged changes need review.",
        "The reviewers' merged changes need review.",
    ],
)
def test_additional_scoped_exceptions_and_evidence(text: str) -> None:
    assert state_guard.unsupported_claims(text) == [text]


@pytest.mark.parametrize(
    "text", ["When it's merged, check again.", "If it merged, check again."]
)
def test_scoped_qualifiers_and_attached_evidence_pass(text: str) -> None:
    assert state_guard.unsupported_claims(text) == [text]


@pytest.mark.parametrize(
    "text", ["It will be merged after review.", "It should have deployed by tomorrow."]
)
def test_review_legitimate_shapes_pass(text: str) -> None:
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


def nested_tags() -> list[str]:
    """Tags nested up to two deep, with every whitespace and bracket variant."""
    gaps = ["", " ", "  "]
    opens = [f"[state-guard:{gap}" for gap in gaps]
    shells = [(o, g + "unverified" + c) for o in opens for g in gaps for c in ["]", ""]]
    inner = [TAG, *(o + g for o, g in shells)]
    return sorted(
        {o + i + t for o, t in shells for i in inner}
        | {o + i + g + t for o, t in shells for i in inner for g in gaps}
        | {"[state-guard:[state-guard: unverified] unverified]"}
    )


NESTED_TAGS = nested_tags()
SPLIT_CLAIMS = [
    "It is in {} place.",
    "It has {} been done.",
    "They have {} been done.",
    "It was rolled {} out.",
]


def codex_nestings() -> set[str]:
    """The generator from the tools#128 review: whitespace around nested tags."""
    found = set()
    for i in [*range(1, 15), 24]:
        for left, right in [("", " "), (" ", ""), (" ", " ")]:
            nested = TAG
            for _ in range(1, 12):
                nested = TAG[:i] + left + nested + right + TAG[i:]
                found.add(nested)
    return found


def test_generated_nestings_leave_the_claim_judged() -> None:
    nestings = codex_nestings()
    assert len(nestings) > 400
    for nesting in nestings:
        assert state_guard.judged_claims(f"{nesting} It was done."), nesting


def test_every_nested_tag_shape_leaves_the_claim_judged() -> None:
    assert len(NESTED_TAGS) > 100
    for tag in NESTED_TAGS:
        for text in (
            f"{tag}It was done.",
            f"It was done {tag}.",
            f"It was {tag} done.",
        ):
            assert state_guard.unsupported_claims(state_guard.strip_tags(text)), text
        assert not state_guard.unsupported_claims(
            state_guard.strip_tags(f"{tag}A note.")
        )


@pytest.mark.parametrize("sentence", SPLIT_CLAIMS)
@pytest.mark.parametrize("tag", [TAG, *NESTED_TAGS[::40]])
def test_a_tag_inside_a_claim_does_not_hide_it(sentence: str, tag: str) -> None:
    assert state_guard.unsupported_claims(state_guard.strip_tags(sentence.format(tag)))


def test_a_tag_in_a_negated_claim_keeps_its_negation() -> None:
    tagged = f"It was {TAG} not done."
    assert state_guard.strip_tags(tagged) == "It was not done."


ZERO_WIDTH = chr(0x200B)
CODEX_TAGS = [
    "[STATE-GUARD: UNVERIFIED]",
    "[State-guard: unverified]",
    "[state-guard: Unverified]",
    "[state-\nguard: unverified]",
    "[state-guard:\nunverified]",
    f"[state-{ZERO_WIDTH}guard: unverified]",
    f"[state-guard:{ZERO_WIDTH}unverified]",
    "[state-guard:\u00a0unverified]",
    "[state-guard:\tunverified]",
    "[state-guard:\r\nunverified]",
    "[state-guard:\u2028unverified]",
    f"[state-guard:{chr(0xFEFF)}unverified]",
]


@pytest.mark.parametrize("tag", CODEX_TAGS)
def test_case_and_invisible_characters_leave_the_tag_unusable(tag: str) -> None:
    assert state_guard.judged_claims(f"{tag} It was done.")
    assert state_guard.judged_claims(f"It was done {tag}.")
    assert not state_guard.judged_claims(f"{tag} A note.")


@pytest.mark.parametrize(
    "text",
    [
        "do{}ne.",
        "It was mer{}ged.",
        "It was d{}one.",
        "It was do{}ne.",
        "It was no{}t merged.",
        "It was no{}t applied.",
        "It was done{}ness.",
    ],
)
@pytest.mark.parametrize("tag", [TAG, *CODEX_TAGS])
def test_a_tag_inside_a_word_is_judged_split_and_joined(text: str, tag: str) -> None:
    judge = state_guard.judged_claims
    assert set(judge(text.format(tag))) == set(judge(text.format(""))) | set(
        judge(text.format(" "))
    )


def test_a_claim_hidden_by_splitting_a_word_is_still_judged() -> None:
    assert state_guard.judged_claims(f"It was do{TAG}ne.")
    assert state_guard.judged_claims(f"It was mer{TAG}ged.")
    assert state_guard.judged_claims(f"It was no{TAG}t merged.")
    assert not state_guard.judged_claims(f"A not{TAG}e.")
    assert state_guard.renderings(f"do{TAG}ne") == ("do ne", "done")


@pytest.fixture(scope="module")
def invisible_characters() -> tuple[dict[str, list[str]], set[str]]:
    """Scan Unicode once, outside the per-test hook execution budget."""
    categories: dict[str, list[str]] = {c: [] for c in ("Cf", "Mn", "Me", "Cc")}
    hidden = set[str]()
    for number in range(0x110000):
        char = chr(number)
        if char.isspace():
            continue
        category = unicodedata.category(char)
        if category in categories:
            categories[category].append(char)
        if state_guard.invisible(char):
            hidden.add(char)
    return categories, hidden


def test_invisible_characters_are_the_ones_that_render_as_nothing(
    invisible_characters: tuple[dict[str, list[str]], set[str]],
) -> None:
    categories, hidden = invisible_characters
    for chars in categories.values():
        assert all(state_guard.invisible(c) for c in chars)
    assert {
        "\u115f",
        "\u1160",
        "\u2800",
        "\u3164",
        chr(0xFFA0),
    } <= hidden
    assert not any(state_guard.invisible(c) for c in "aZ0 .-:[]\n\u00e9\u2014")
    assert state_guard.visible("a\u200bb\ufe0fc\x00d") == "abcd"


INVISIBLE_IN_TAG = [
    "\u115f",
    "\u1160",
    "\u3164",
    chr(0xFFA0),
    "\u2800",
    "\ufe0f",
    "\u034f",
    "\u180b",
    "\u0301",
    "\x01",
    chr(0xE0100),
]


@pytest.mark.parametrize("char", INVISIBLE_IN_TAG)
def test_invisible_characters_in_a_tag_leave_the_claim_judged(char: str) -> None:
    for tag in (
        f"[state-{char}guard: unverified]",
        f"[state-guard:{char} unverified]",
        f"[state-guard: un{char}verified]",
        f"[state-guard: unverified{char}]",
    ):
        assert state_guard.judged_claims(f"{tag} It was done."), tag
        assert state_guard.judged_claims(f"It was do{tag}ne."), tag
        assert not state_guard.judged_claims(f"{tag} A note."), tag


@pytest.mark.parametrize("gap", [" ", "\n", "\u200b", "\t", "\u00a0", "\r\n"])
def test_a_gap_inside_the_word_leaves_the_claim_judged(gap: str) -> None:
    tag = f"[state-guard: un{gap}verified]"
    assert state_guard.judged_claims(f"It was do{tag}ne.")
    assert state_guard.judged_claims(f"{tag} It was done.")
    assert state_guard.judged_claims(f"It was {tag}done.")
    assert not state_guard.judged_claims(f"A not{tag}e.")


@pytest.mark.parametrize(
    "text",
    [
        "[state-\nguard: unverified] It was done.",
        "[state-guard:\nunverified] It was done.",
        "It is in [state-\nguard: unverified] place.",
        "It was do[state-\nguard: unverified]ne.",
        "It was do[state-guard: un\nverified]ne.",
        "[state-gu\n\nard: unverified]\nIt was done.",
    ],
)
def test_a_tag_cut_by_a_line_break_is_tagged_not_passed(text: str) -> None:
    assert state_guard.judged_claims(text)
    tagged, sentences = state_guard.tag_sentences(text)
    assert tagged != text
    assert TAG in tagged
    assert sentences


def test_a_tag_across_a_line_break_keeps_its_sentence_whole() -> None:
    tagged, sentences = state_guard.tag_sentences(
        "Intro.\n[state-\nguard: unverified] It was done.\nEnd."
    )
    assert tagged == (f"Intro.\n{TAG} [state-\nguard: unverified] It was done.\nEnd.")
    assert sentences == ["[state-\nguard: unverified] It was done."]


@pytest.mark.parametrize(
    "text",
    [
        "Intro.\n[state-\n[state-guard: unverified]guard: unverified] It was done.",
        "Intro.\nIt was do[state-\n[state-guard: unverified]guard: unverified]ne.",
    ],
)
def test_a_claim_no_sentence_holds_tags_the_whole_message(text: str) -> None:
    assert state_guard.judged_claims(text)
    tagged, sentences = state_guard.tag_sentences(text)
    assert tagged == f"{TAG} {text}"
    assert sentences[-1] == text.strip()


def test_deeply_nested_tags_are_undecidable_and_fast() -> None:
    nested = TAG
    for _ in range(state_guard.MAX_PASSES + 5):
        nested = f"[state-guard: unverified {nested}]"
    assert state_guard.judged_claims(nested) == [state_guard.UNDECIDABLE]
    assert state_guard.judged_claims(f"It was done. {nested}") == [
        state_guard.UNDECIDABLE
    ]
    tagged, sentences = state_guard.tag_sentences(nested)
    assert tagged.startswith(TAG)
    assert sentences
    assert state_guard.rule_of(nested) == "undecidable"


def test_an_oversized_message_is_undecidable() -> None:
    big = "A note. " * (state_guard.MAX_JUDGED // 8 + 1)
    assert state_guard.judged_claims(big) == [state_guard.UNDECIDABLE]
    tagged, sentences = state_guard.tag_sentences(big)
    assert tagged == f"{TAG} {big}"
    assert sentences == [big.strip()[: state_guard.MAX_QUOTED]]


def test_nesting_within_the_pass_limit_is_still_judged() -> None:
    nested = TAG
    for _ in range(12):
        nested = f"[state-guard: unverified {nested}]"
    assert state_guard.judged_claims(f"{nested} It was done.") == ["It was done."]
    assert state_guard.rule_of("It was done.") == "was done"


@pytest.mark.parametrize("tag", [TAG, *NESTED_TAGS[::25]])
def test_nested_sender_tags_cannot_preempt_the_guard(run: Runner, tag: str) -> None:
    assert state_guard.strip_tags(f"{tag}It was done.") == " It was done."
    assert state_guard.strip_tags(f"It was done {tag}.") == "It was done ."
    assert_tagged(run(send(f"{tag}It was done.")), f"{TAG} {tag}It was done.")
    assert run(send(f"{tag}A note.")) == (0, "", "")
    assert_tagged(run(send(f"It was done {tag}.")), f"{TAG} It was done {tag}.")


@pytest.mark.parametrize("tag", [TAG, TAG + " ", TAG + " " + TAG + " "])
def test_stop_rejects_copied_tags(run: Runner, tag: str) -> None:
    status, out, err = run(stop(last_assistant_message=f"{tag}It was done."))
    assert (status, err) == (0, "")
    assert json.loads(out) == {
        "decision": "block",
        "reason": state_guard.final_claim_reason(["It was done."]),
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
    assert_tagged(
        run(send("It was done. It shipped.")),
        f"{TAG} It was done. {TAG} It shipped.",
    )


def test_long_sentences_preserve_full_text_and_evidence(run: Runner) -> None:
    sentence = "It was done " + "x" * 200
    message = f"{sentence}. {sentence}. {sentence} in #12."
    assert_tagged(
        run(send(message)), f"{TAG} {sentence}. {TAG} {sentence}. {sentence} in #12."
    )


def refused(result: Result) -> bool:
    status, out, err = result
    return (
        (status, out) == (2, "")
        and "was not sent. Offending:" in err
        and "ignoring the config" not in err
    )


@pytest.mark.parametrize(
    "text",
    [
        None,
        "[state_guard]\n",
        '[state_guard]\nother = "value"\n',
        "[state_guard]\ntag_messages = false\n",
        '[state_guard]\ntag_messages = "false"\n',
        '[state_guard]\ntag_messages = "true"\n',
        "[state_guard]\ntag_messages = 0\n",
        "[state_guard]\ntag_messages = 1\n",
        "[state_guard]\ntag_messages = {}\n",
    ],
)
def test_only_boolean_true_turns_tagging_on(
    config_file: Path, run: Runner, text: str | None
) -> None:
    if text is None:
        config_file.unlink()
    else:
        config_file.write_text(text)
    assert refused(run(send("It was done.")))


@pytest.mark.parametrize("text", ["state_guard = 1\n", "[state_guard\n"])
def test_a_config_error_refuses_and_says_why(
    config_file: Path, run: Runner, text: str
) -> None:
    config_file.write_text(text)
    status, out, err = run(send("It was done."))
    assert refused((status, out, err))


def write_invalid_utf8(path: Path) -> None:
    path.write_bytes(b"\xff\xfe")


def make_directory(path: Path) -> None:
    path.mkdir()


def unreadable(path: Path) -> None:
    path.write_text("[state_guard]\ntag_messages = true\n")
    path.chmod(0)


@pytest.mark.parametrize(
    "make",
    [
        write_invalid_utf8,
        make_directory,
        os.mkfifo,
        unreadable,
    ],
    ids=["invalid-utf8", "directory", "fifo", "unreadable"],
)
def test_a_config_that_cannot_be_read_refuses(
    config_file: Path, run: Runner, make: Callable[[Path], object]
) -> None:
    config_file.unlink()
    make(config_file)
    status, out, err = run(send("It was done."))
    assert refused((status, out, err))


def test_a_stdout_failure_keeps_the_refusal(
    monkeypatch: pytest.MonkeyPatch, run: Runner
) -> None:
    class Broken(io.StringIO):
        @override
        def write(self, _text: str) -> int:
            message = "stdout closed"
            raise BrokenPipeError(message)

    monkeypatch.setattr(sys, "stdout", Broken())
    assert refused(run(send("It was done.")))


def hook_child(
    config_file: Path, message: str, stdout: int = subprocess.PIPE
) -> subprocess.CompletedProcess[str]:
    """Run the hook in a child that the parent kills on a deadline.

    The in-process alarm turns a hang into a caught error, so a hang test needs
    a child. The one-second alarm of the `_bounded_hook` fixture would also end a
    slow child start, so the child's own 20-second deadline replaces it.
    """
    signal.alarm(0)
    return subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; from tbhbdev.alpha import state_guard;"
                " sys.exit(state_guard.main())"
            ),
        ],
        input=json.dumps(send(message)),
        stdout=stdout,
        stderr=subprocess.PIPE,
        text=True,
        timeout=20,
        check=False,
        env={**os.environ, "XDG_CONFIG_HOME": str(config_file.parent.parent)},
    )


def test_a_config_that_is_a_pipe_cannot_hang_the_hook(config_file: Path) -> None:
    config_file.unlink()
    os.mkfifo(config_file)
    done = hook_child(config_file, "It was done.")
    assert refused((done.returncode, done.stdout, done.stderr))


def test_a_log_that_is_a_pipe_cannot_hang_the_hook(
    tmp_path: Path, config_file: Path
) -> None:
    directory = tmp_path / "state/tbhbdev/state-guard"
    directory.mkdir(parents=True)
    os.mkfifo(directory / "tagged.jsonl")
    done = hook_child(config_file, "It was done.")
    assert (done.returncode, done.stderr) == (0, "")
    assert json.loads(done.stdout)["hookSpecificOutput"]["updatedInput"] == {
        "message": f"{TAG} It was done."
    }


def padded(config_file: Path, size: int) -> str:
    """A flagged message whose tagged hook output is SIZE bytes with the newline."""
    base = len(hook_child(config_file, UNSUPPORTED_CLAIM + " x.").stdout.encode())
    return UNSUPPORTED_CLAIM + " " + "x" * (size - base + 1) + "."


def fill_pipe(read_fd: int, write_fd: int, free: int) -> None:
    """Fill the pipe, then drain FREE bytes, so exactly that much space is left."""
    os.set_blocking(write_fd, False)
    try:
        while True:
            os.write(write_fd, b"." * 4096)
    except BlockingIOError:
        pass
    os.set_blocking(write_fd, True)
    os.read(read_fd, free)


@pytest.mark.parametrize(("extra", "tagged"), [(0, True), (1, False)])
def test_the_tagged_output_cap_never_blocks_on_a_pipe_nobody_reads(
    config_file: Path, extra: int, tagged: bool
) -> None:
    read_fd, write_fd = os.pipe()
    try:
        fill_pipe(read_fd, write_fd, 4096)
        done = hook_child(
            config_file,
            padded(config_file, state_guard.MAX_TAGGED_OUTPUT + extra),
            write_fd,
        )
    finally:
        os.close(write_fd)
        leftover = os.read(read_fd, 1 << 20)
        os.close(read_fd)
    if tagged:
        assert (done.returncode, done.stderr) == (0, "")
        assert leftover.endswith(b"}\n")
    else:
        assert refused((done.returncode, "", done.stderr))
        assert set(leftover) == {ord(".")}


def test_a_tagged_message_too_large_to_write_keeps_the_refusal(
    config_file: Path, run: Runner
) -> None:
    status, out, err = run(send(UNSUPPORTED_CLAIM + " " + "x" * 9000 + "."))
    assert refused((status, out, err))
    assert not (config_file.parent / "tagged.jsonl").exists()


def test_a_failure_while_tagging_keeps_the_refusal(
    monkeypatch: pytest.MonkeyPatch, run: Runner
) -> None:
    def broken(_text: str) -> tuple[str, list[str]]:
        message = "tagging broke"
        raise RuntimeError(message)

    monkeypatch.setattr(state_guard, "tag_sentences", broken)
    assert refused(run(send("It was done.")))


def test_a_config_error_does_not_stop_a_clean_message(
    config_file: Path, run: Runner
) -> None:
    config_file.write_text("[state_guard\n")
    assert run(send("Hello.")) == (0, "", "")


def test_log_refuses_symlinks_and_special_files(tmp_path: Path, run: Runner) -> None:
    directory = tmp_path / "state/tbhbdev/state-guard"
    directory.mkdir(parents=True)
    target = tmp_path / "target"
    target.write_text("keep\n")
    target.chmod(0o644)
    (directory / "tagged.jsonl").symlink_to(target)
    status, out, err = run(send("It was done."))
    assert_tagged((status, out, err), f"{TAG} It was done.")
    assert (target.read_text(), target.stat().st_mode & 0o777) == ("keep\n", 0o644)
    (directory / "tagged.jsonl").unlink()
    os.mkfifo(directory / "tagged.jsonl")
    status, out, err = run(send("It was done."))
    assert_tagged((status, out, err), f"{TAG} It was done.")


def test_log_refuses_a_hard_linked_file(tmp_path: Path, run: Runner) -> None:
    directory = tmp_path / "state/tbhbdev/state-guard"
    directory.mkdir(parents=True)
    target = tmp_path / "target"
    target.write_text("keep\n")
    os.link(target, directory / "tagged.jsonl")
    assert_tagged(run(send("It was done.")), f"{TAG} It was done.")
    assert target.read_text() == "keep\n"


def test_log_refuses_a_symlinked_directory(tmp_path: Path, run: Runner) -> None:
    parent = tmp_path / "state/tbhbdev"
    parent.mkdir(parents=True)
    target = tmp_path / "elsewhere"
    target.mkdir()
    target.chmod(0o755)
    (parent / "state-guard").symlink_to(target)
    status, out, err = run(send("It was done."))
    assert_tagged((status, out, err), f"{TAG} It was done.")
    assert list(target.iterdir()) == []
    assert target.stat().st_mode & 0o777 == 0o755


def test_log_refuses_a_file_owned_by_another_user(
    monkeypatch: pytest.MonkeyPatch, run: Runner
) -> None:
    monkeypatch.setattr(os, "getuid", lambda: os.geteuid() + 1)
    status, out, err = run(send("It was done."))
    assert_tagged((status, out, err), f"{TAG} It was done.")


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

    def observe(
        path: str | Path, flags: int, mode: int = 0o777, *, dir_fd: int | None = None
    ) -> int:
        fd = original(path, flags, mode, dir_fd=dir_fd)
        if dir_fd is not None:
            modes.append(os.fstat(fd).st_mode & 0o777)
        return fd

    monkeypatch.setattr(os, "open", observe)
    previous = os.umask(0)
    try:
        assert_tagged(run(send("It was done.")), f"{TAG} It was done.")
    finally:
        os.umask(previous)
    assert modes == [0o600]


def test_log_failures_write_nothing_to_stderr(
    monkeypatch: pytest.MonkeyPatch, run: Runner
) -> None:
    def broken(_path: Path) -> int:
        message = "one\ntwo"
        raise RuntimeError(message)

    monkeypatch.setattr(state_guard, "open_log", broken)
    assert_tagged(run(send("It was done.")), f"{TAG} It was done.")


@pytest.mark.parametrize("tool", ["Read", "Write", "Edit", "Bash", "Other"])
def test_tags_never_touch_other_tools(run: Runner, tool: str) -> None:
    data = send(UNSUPPORTED_CLAIM)
    data["tool_name"] = tool
    assert run(data) == (0, "", "")
    assert data["tool_input"] == {"message": UNSUPPORTED_CLAIM}


@pytest.mark.parametrize("event", ["PostToolUse", "Other", None])
def test_other_hook_events_refuse_instead_of_tagging(
    run: Runner, event: str | None
) -> None:
    data = send(UNSUPPORTED_CLAIM)
    data["hook_event_name"] = event
    assert refused(run(data))
    assert data["tool_input"] == {"message": UNSUPPORTED_CLAIM}


@pytest.mark.parametrize("blank", sorted(state_guard.BLANKS))
def test_a_blank_filler_between_words_is_a_word_boundary(blank: str) -> None:
    assert state_guard.visible(f"done{blank}next") == "done next"
    assert state_guard.judged_claims(f"It was done{blank}next.") == [
        "It was done next."
    ]
    assert state_guard.visible("do\u200bne") == "done"


def test_a_repeated_claim_glued_to_a_nested_tag_is_tagged() -> None:
    text = (
        "It was done.\nIt was done.[state-\n[state-guard: unverified]guard: unverified]"
    )
    assert len(state_guard.judged_claims(text)) == 2
    tagged, sentences = state_guard.tag_sentences(text)
    assert tagged.count(TAG) >= 2 or tagged == f"{TAG} {text}"
    assert sentences


def test_a_message_of_exactly_the_judging_limit_is_still_judged() -> None:
    text = "x" * state_guard.MAX_JUDGED
    assert state_guard.tag_sentences(text) == (text, [])
    tagged, sentences = state_guard.tag_sentences(text + "x")
    assert tagged == f"{TAG} {text}x"
    assert sentences

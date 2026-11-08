# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns
"""Tests for tbhbdev alpha state-guard, against a config in a temporary XDG dir."""

import io
import json
import signal
from collections.abc import Callable, Iterator
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


def test_an_unsupported_claims_claim_is_blocked(run: Runner) -> None:
    status, out, err = run(send(UNSUPPORTED_CLAIM))
    assert (status, out) == (2, "")
    assert err == (
        "state-guard: this message makes a status claim without evidence. Add "
        "the PR number and sha, the merge sha, command output or a log path, "
        "attribute it ('ops says ...'), or say it is unverified. Offending: "
        f"{UNSUPPORTED_CLAIM}\n"
    )


def test_at_most_three_offenders_are_listed_and_each_is_cut(run: Runner) -> None:
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
        "Ops says it was applied.",
        "Ops said it was applied.",
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
    assert run(send(claim))[0] == 2


def test_evidence_in_another_sentence_does_not_cover_a_claim(run: Runner) -> None:
    status, _, err = run(send("It was applied. See #12 for the other one."))
    assert status == 2
    assert err.endswith("Offending: It was applied.\n")


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


C = "git" + " commit"  # the live hook's own test table, as vectors
LIVE_TABLE = {
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


@pytest.mark.parametrize(("command", "status"), LIVE_TABLE.items())
def test_the_port_agrees_with_the_live_hooks_table(
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
    assert state_guard.pre_tool_reason({"tool_name": tool, "tool_input": raw}) is None
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
    assert run(bash("git commit -mfixes")) == (0, "", "")
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
def test_noncommands_inside_quotes_stay_allowed(run: Runner, command: str) -> None:
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
        "1234567",
        "12345678901234567890",
        "abcdefa",
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
    assert run(send(f"It was merged in {candidate}."))[0] == 2


@pytest.mark.parametrize(
    "candidate", ["a123456", "123456a", "A123456", "123456A", "a" + "1" * 39]
)
def test_hash_evidence_accepts_both_cases_and_length_limits(
    run: Runner, candidate: str
) -> None:
    assert run(send(f"It was merged in {candidate}.")) == (0, "", "")

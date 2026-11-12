"""The approval hook accepts only the operator's exact command form."""

import io
import json
import sys

import pytest

from tbhbdev import alpha
from tbhbdev.alpha import guard_approve as guard

SHA = "0123456789abcdef0123456789abcdef01234567"
COMMAND = f"~/.local/bin/tbhb-approve tools 224 {SHA}"
REASON = (
    "Use exactly ~/.local/bin/tbhb-approve <repo> <pr> <sha>. "
    "Run it alone as one command."
)


@pytest.mark.parametrize(
    "command",
    [
        COMMAND,
        f" \t\n{COMMAND}\n\t ",
        f"~/.local/bin/tbhb-approve\tTools_1.2-a\t1\t{SHA}",
        f"~/.local/bin/tbhb-approve  tools  224  {SHA}",
        "true",
        "",
        "grep approve notes.md",
    ],
)
def test_allowed(command: str) -> None:
    assert not guard.denied(command)


@pytest.mark.parametrize(
    "command",
    [
        *(
            f"{COMMAND}{separator}true"
            for separator in (";", "&&", "||", "|", "&", "\n")
        ),
        *(f"{COMMAND}{separator}" for separator in (";", "&", "\ntrue")),
        *(f"{COMMAND}{padding}" for padding in ("\r", "\xa0", "\x0b", "\x85", "\x1c")),
        f"\xa0{COMMAND}",
        f"{COMMAND}X",
        *(
            f"~/.local/bin/tbhb-approve {repo} 224 {SHA}"
            for repo in ("-h", ".", "..", ".hidden", "-", "_")
        ),
        f"NAME=value {COMMAND}",
        f"export NAME=value; {COMMAND}",
        f"env {COMMAND}",
        f"command {COMMAND}",
        f"({COMMAND})",
        f"{{ {COMMAND}; }}",
        f"$({COMMAND})",
        f"`{COMMAND}`",
        f"echo $({COMMAND})",
        f"{COMMAND} > output",
        f"{COMMAND} < input",
        f"{COMMAND} 2>&1",
        f"{COMMAND} <<EOF\ndata\nEOF",
        f"{COMMAND} # comment",
        f"{COMMAND}#comment",
        "~/.local/bin/tbhb-approve tools 224",
        f"~/.local/bin/tbhb-approve tools 224 {SHA[:-1]}",
        f"~/.local/bin/tbhb-approve tools 224 {SHA}0",
        f"~/.local/bin/tbhb-approve tools 224 {SHA.upper()}",
        f"~/.local/bin/tbhb-approve tools 224 {'g' * 40}",
        f"~/.local/bin/tbhb-approve owner/tools 224 {SHA}",
        f"~/.local/bin/tbhb-approve tools+more 224 {SHA}",
        f"~/.local/bin/tbhb-approve tools{chr(233)} 224 {SHA}",
        f"~/.local/bin/tbhb-approve tools 0 {SHA}",
        f"~/.local/bin/tbhb-approve tools 0224 {SHA}",
        f"~/.local/bin/tbhb-approve tools -1 {SHA}",
        f"~/.local/bin/tbhb-approve tools 1.5 {SHA}",
        f"~/.local/bin/tbhb-approve tools ٢ {SHA}",
        f"{COMMAND} extra",
        f"$HOME/.local/bin/tbhb-approve tools 224 {SHA}",
        f"tbhb-approve tools 224 {SHA}",
        f"/home/user/.local/bin/tbhb-approve tools 224 {SHA}",
        f"'~/.local/bin/tbhb-approve' tools 224 {SHA}",
        f'"~/.local/bin/tbhb-approve" tools 224 {SHA}',
        f"\\~/.local/bin/tbhb-approve tools 224 {SHA}",
        f"~/.local/bin/tbhb-approve 'tools' 224 {SHA}",
        f"~/.local/bin/tbhb-approve tools '224' {SHA}",
        f"~/.local/bin/tbhb-approve tools 224 '{SHA}'",
        f"~/.local/bin/tbhb-approve tools\\\n 224 {SHA}",
        f"~/.local/bin/tbhb-approve tools\n224 {SHA}",
        f"~/.local/bin/tbhb-approve tools\r224 {SHA}",
        f"~/.local/bin/tbhb-approve tools\v224 {SHA}",
        "export GH_CONFIG_DIR=/tmp/config; gh query example; tbhb-approve tools 224",
        "grep tbhb-approve x",
        "echo 'tbhb-approve'",
        "# tbhb-approve",
        "prefix-tbhb-approve-suffix",
        "tbhb-approve",
    ],
)
def test_denied(command: str) -> None:
    assert guard.denied(command)


@pytest.mark.parametrize("agy", [False, True])
def test_hook_decision(
    agy: bool, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    payload = (
        {"toolCall": {"args": {"CommandLine": "grep tbhb-approve x"}}}
        if agy
        else {"tool_input": {"command": "grep tbhb-approve x"}}
    )
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    assert alpha.main(["guard-approve"], "tbhbdev alpha") == 0
    expected = (
        {"decision": "deny", "reason": REASON}
        if agy
        else {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": REASON,
            }
        }
    )
    assert json.loads(capsys.readouterr().out) == expected


@pytest.mark.parametrize(
    "payload",
    [
        "",
        "invalid",
        "[]",
        "null",
        "{}",
        '{"tool_input":null}',
        '{"tool_input":{"command":123}}',
        '{"tool_input":{"command":"true"}}',
        json.dumps({"tool_input": {"command": COMMAND}}),
    ],
)
def test_silent_allow(
    payload: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys, "stdin", io.StringIO(payload))
    assert guard.main() == 0
    assert capsys.readouterr() == ("", "")


@pytest.mark.parametrize("error", [OSError, RuntimeError])
def test_unreadable_stdin(
    error: type[Exception],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def unreadable() -> dict[str, object]:
        raise error

    monkeypatch.setattr(guard, "read_hook_stdin", unreadable)
    assert guard.main() == 0
    assert capsys.readouterr() == ("", "")


def test_dispatch_returns_hook_status(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(guard, "main", lambda: 7)
    assert alpha.main(["guard-approve"], "tbhbdev alpha") == 7


def test_help_lists_hook(capsys: pytest.CaptureFixture[str]) -> None:
    assert alpha.main(["--help"], "tbhbdev alpha") == 0
    assert (
        "  guard-approve PreToolUse hook: require the exact approval command form\n"
        in capsys.readouterr().out
    )

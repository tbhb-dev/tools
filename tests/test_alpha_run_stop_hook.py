"""Exercise run completion through the installed hook command."""

import io
import json
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from agent_host_example import commit, git, repository

from tbhbdev.alpha import main, run_stop_hook

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

CLAUDE = {"session_id": "example", "hook_event_name": "Stop", "stop_hook_active": True}
CODEX = {**CLAUDE, "turn_id": "turn", "transcript_path": None}
AGY = {
    "conversationId": "example",
    "hookEventName": "Stop",
    "terminationReason": "NO_TOOL_CALL",
    "continuationActive": True,
}


@pytest.fixture
def run(tmp_path: Path) -> run_stop_hook.StopRun:
    return {
        "profile": "review",
        "workspace": str(tmp_path / "clone"),
        "reference": "refs/heads/agent/example",
        "start": None,
        "counter": str(tmp_path / "stop-count"),
        "log": str(tmp_path / "log"),
    }


@pytest.fixture
def stop(
    run: run_stop_hook.StopRun,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> Callable[[str, Mapping[str, object]], str]:
    def call(harness: str, payload: Mapping[str, object]) -> str:
        monkeypatch.setenv(run_stop_hook.ENV, json.dumps(run))
        monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
        assert main(["run-stop-hook", harness], "tbhbdev alpha") == 0
        return capsys.readouterr().out

    return call


def test_interactive_stop_never_reads_stdin(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(run_stop_hook.ENV, raising=False)
    monkeypatch.setattr("sys.stdin", None)
    assert main(["run-stop-hook", "claude"], "tbhbdev alpha") == 0


@pytest.mark.parametrize(("harness", "payload"), [("claude", CLAUDE), ("codex", CODEX)])
def test_review_uses_current_reply_and_limits_retries(
    harness: str,
    payload: Mapping[str, object],
    stop: Callable[[str, Mapping[str, object]], str],
    run: run_stop_hook.StopRun,
) -> None:
    for verdict in ("GO", "BLOCK " + "a" * 40):
        assert (
            stop(
                harness,
                {**payload, "last_assistant_message": f"Done\nVERDICT: {verdict}\n"},
            )
            == ""
        )
    for message in (None, "Asking for approval", "Quoted VERDICT: GO\nVERDICT: GONE"):
        result = json.loads(
            stop(harness, {**payload, "last_assistant_message": message})
        )
        assert result["decision"] == "block"
        assert "Finish the review" in result["reason"]
    assert stop(harness, payload) == ""
    assert Path(run["counter"]).read_text() == "3"
    assert (
        "allowing stop after 3 blocks. Finish the review"
        in Path(run["log"]).read_text()
    )


def test_agy_reads_final_planner_output(
    stop: Callable[[str, Mapping[str, object]], str],
    tmp_path: Path,
) -> None:
    compact = tmp_path / "transcript.jsonl"
    payload = {**AGY, "transcriptPath": str(compact)}
    assert stop("antigravity", {**payload, "terminationReason": "USER_CANCELED"}) == ""
    for incomplete in (AGY, payload):
        assert json.loads(stop("antigravity", incomplete))["decision"] == "continue"
    verdict = '{"type":"PLANNER_RESPONSE","content":"VERDICT: GO"}\n'
    compact.write_text(verdict)
    assert stop("antigravity", payload) == ""
    full = tmp_path / "transcript_full.jsonl"
    full.write_text(verdict + '{"type":"PLANNER_RESPONSE","tool_calls":[]}\n')
    assert json.loads(stop("antigravity", payload))["decision"] == "continue"
    full.write_text(verdict + '{"type":"USER_INPUT","content":"unrelated"}\npartial')
    assert (
        stop("antigravity", {**payload, "terminationReason": "TERMINAL_STEP_TYPE"})
        == ""
    )


def test_build_checks_tracked_changes_and_the_run_branch(
    run: run_stop_hook.StopRun,
    stop: Callable[[str, Mapping[str, object]], str],
    tmp_path: Path,
) -> None:
    _, clone = repository(tmp_path)
    git(clone, "switch", "-c", "agent/example")
    run.update(profile="build", start=git(clone, "rev-parse", "HEAD"))
    assert "commit the result" in json.loads(stop("codex", CODEX))["reason"]
    (clone / "ignored-draft").write_text("untracked")
    start = run["start"]
    run["start"] = None
    assert stop("codex", CODEX) == ""
    (clone / "tracked").write_text("changed")
    git(clone, "add", "tracked")
    assert (
        "commit the intended tracked changes"
        in json.loads(stop("codex", CODEX))["reason"]
    )
    run["start"] = start
    commit(clone, "finished")
    assert stop("codex", CODEX) == ""


def test_git_failure_is_actionable_and_counter_failure_allows_stop(
    run: run_stop_hook.StopRun,
    stop: Callable[[str, Mapping[str, object]], str],
) -> None:
    run["profile"] = "build"
    assert "Restore the run worktree" in json.loads(stop("claude", CLAUDE))["reason"]
    Path(run["counter"]).write_text("corrupt")
    assert stop("claude", CLAUDE) == ""

# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

"""Tests for the opened ledger and the root session behind a caller."""

import fcntl
import json
import os
import stat
import subprocess
from pathlib import Path

import pytest

from tbhbdev.alpha import identity as ai
from tbhbdev.alpha import opened

ROOT = ai.sha256_64("session-raw")
OTHER = ai.sha256_64("other-raw")
CONV = "conv-raw"
SUB = "sub-raw"


@pytest.fixture(autouse=True)
def state(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """A temp user store, and no harness variables."""
    prefixes = ("CLAUDE", "CODEX", "ANTIGRAVITY", "ASCM_")
    list(map(monkeypatch.delenv, [n for n in os.environ if n.startswith(prefixes)]))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    return tmp_path / "state" / "tbhbdev" / "tbhb-alpha" / "opened"


def test_the_ledger_path_is_the_root_hex_in_the_user_store(state: Path) -> None:
    assert opened.ledger_path(ROOT) == state / f"{ai.hx(ROOT)}.json"


def test_no_ledger_file_is_an_empty_ledger() -> None:
    assert opened.read_ledger(ROOT) == set()


def test_record_sorts_deduplicates_and_keeps_entries(state: Path) -> None:
    opened.record_opened(ROOT, ["o/b#2", "o/a#9", "o/b#2"])
    opened.record_opened(ROOT, ["o/a#1", "o/a#9"])
    path = state / f"{ai.hx(ROOT)}.json"
    assert json.loads(path.read_text()) == {"prs": ["o/a#1", "o/a#9", "o/b#2"]}
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert opened.read_ledger(ROOT) == {"o/a#1", "o/a#9", "o/b#2"}
    assert opened.read_ledger(OTHER) == set()
    assert (state / "lock").exists()


@pytest.mark.parametrize("xdg", [None, ""])
def test_record_uses_the_home_state_directory_when_xdg_is_unset(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, xdg: str | None
) -> None:
    if xdg is None:
        monkeypatch.delenv("XDG_STATE_HOME")
    else:
        monkeypatch.setenv("XDG_STATE_HOME", xdg)
    opened.record_opened(ROOT, ["o/a#1"])
    path = (
        tmp_path
        / "home"
        / ".local"
        / "state"
        / "tbhbdev"
        / "tbhb-alpha"
        / "opened"
        / f"{ai.hx(ROOT)}.json"
    )
    assert json.loads(path.read_text()) == {"prs": ["o/a#1"]}
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_record_replaces_the_whole_ledger_under_the_lock(
    state: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    opened.record_opened(ROOT, ["o/a#1"])
    path = opened.ledger_path(ROOT)
    before = path.read_bytes()
    replace = Path.replace
    replaced: list[Path] = []

    def observe(temp: Path, target: Path) -> Path:
        assert target == path
        assert temp != path
        assert temp.parent == path.parent
        assert stat.S_IMODE(temp.stat().st_mode) == 0o600
        assert path.read_bytes() == before
        assert json.loads(temp.read_text()) == {"prs": ["o/a#1", "o/b#2"]}
        with (
            (state / "lock").open("a") as handle,
            pytest.raises(BlockingIOError),
        ):
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        replaced.append(temp)
        return replace(temp, target)

    monkeypatch.setattr(Path, "replace", observe)
    opened.record_opened(ROOT, ["o/b#2"])
    assert len(replaced) == 1
    assert not replaced[0].exists()
    assert json.loads(path.read_text()) == {"prs": ["o/a#1", "o/b#2"]}
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_a_failed_replace_preserves_the_ledger_and_releases_the_lock(
    state: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    opened.record_opened(ROOT, ["o/a#1"])
    path = opened.ledger_path(ROOT)
    before = path.read_bytes()

    def fail(_temp: Path, _target: Path) -> Path:
        raise OSError(28, "No space left on device")

    with monkeypatch.context() as patch:
        patch.setattr(Path, "replace", fail)
        with pytest.raises(OSError, match="No space left on device"):
            opened.record_opened(ROOT, ["o/b#2"])
    assert path.read_bytes() == before
    with (state / "lock").open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    opened.record_opened(ROOT, ["o/c#3"])
    assert json.loads(path.read_text()) == {"prs": ["o/a#1", "o/c#3"]}


@pytest.mark.parametrize(
    ("text", "why"),
    [
        ("not json", "is not JSON"),
        ("[]", "holds no list of pull requests"),
        ('{"prs": "o/a#1"}', "holds no list of pull requests"),
        ('{"prs": ["o/a#1", 2]}', "holds no list of pull requests"),
        ("{}", "holds no list of pull requests"),
    ],
)
def test_a_corrupt_ledger_raises_and_is_left_alone(
    state: Path, text: str, why: str
) -> None:
    path = state / f"{ai.hx(ROOT)}.json"
    state.mkdir(parents=True)
    path.write_text(text)
    with pytest.raises(opened.LedgerError, match=f"^{path} {why}$"):
        opened.read_ledger(ROOT)
    with pytest.raises(opened.LedgerError):
        opened.record_opened(ROOT, ["o/a#1"])
    assert path.read_text() == text


def test_an_unreadable_ledger_raises(state: Path) -> None:
    path = state / f"{ai.hx(ROOT)}.json"
    path.mkdir(parents=True)
    with pytest.raises(
        opened.LedgerError, match=f"^cannot read {path}: Is a directory$"
    ):
        opened.read_ledger(ROOT)


def test_an_empty_ledger_list_is_valid(state: Path) -> None:
    state.mkdir(parents=True)
    (state / f"{ai.hx(ROOT)}.json").write_text('{"prs": []}')
    assert opened.read_ledger(ROOT) == set()


# ------------------------------------------------------------------ roots


def make_brain(tmp_path: Path, *names: str) -> Path:
    brain = tmp_path / "brain"
    for cid in names:
        (brain / cid).mkdir(parents=True)
    return brain


def adopt(brain: Path, parent: str, child: str) -> None:
    """Make ``child`` a subagent of ``parent`` in the brain directory."""
    folder = brain / child / ".system_generated" / "messages"
    folder.mkdir(parents=True, exist_ok=True)
    tool = {"toolCall": {"name": "invoke_subagent"}}
    message = {"recipient": child, "sender": parent, "sourceMetadata": {"tool": tool}}
    (folder / "1.json").write_text(json.dumps(message))


def test_agy_root_hash_needs_the_conversation_in_a_brain(tmp_path: Path) -> None:
    assert opened.agy_root_hash(CONV, None) is None
    brain = make_brain(tmp_path, "someone-else")
    assert opened.agy_root_hash(CONV, brain) is None
    (brain / CONV).mkdir()
    assert opened.agy_root_hash(CONV, brain) == ai.sha256_64(CONV)
    adopt(brain, CONV, SUB)
    assert opened.agy_root_hash(SUB, brain) == ai.sha256_64(CONV)


def test_caller_root_of_claude_and_codex_is_the_session_id() -> None:
    assert opened.caller_root({"session_id": "session-raw"}, {}) == ROOT
    child: dict[str, object] = {"session_id": "session-raw", "agent_id": "agent-raw"}
    assert opened.caller_root(child, {}) == ROOT
    assert opened.caller_root({"sessionId": "session-raw"}, {}) == ROOT


@pytest.mark.parametrize(
    "data", [{}, {"session_id": ""}, {"session_id": 7}, {"tool_input": {}}]
)
def test_caller_root_without_a_session_is_none(data: dict[str, object]) -> None:
    env = {"CLAUDE_CODE_SESSION_ID": "session-raw"}
    assert opened.caller_root(data, env) is None


def test_caller_root_of_antigravity_follows_the_artifact_directory(
    tmp_path: Path,
) -> None:
    brain = make_brain(tmp_path, CONV)
    adopt(brain, CONV, SUB)
    data: dict[str, object] = {
        "toolCall": {},
        "conversationId": SUB,
        "artifactDirectoryPath": str(brain / SUB),
    }
    assert opened.caller_root(data, {}) == ai.sha256_64(CONV)
    data["artifactDirectoryPath"] = str(tmp_path / "elsewhere" / SUB)
    assert opened.caller_root(data, {}) is None


def test_caller_root_of_antigravity_falls_back_to_the_env_and_record(
    tmp_path: Path,
) -> None:
    brain = make_brain(tmp_path, CONV, SUB)
    adopt(brain, CONV, SUB)
    data: dict[str, object] = {"toolCall": {}}
    assert opened.caller_root(data, {}) is None
    env = {"ANTIGRAVITY_CONVERSATION_ID": SUB}
    assert opened.caller_root(data, env) is None
    env["ANTIGRAVITY_APP_DATA_DIR"] = str(tmp_path)
    assert opened.caller_root(data, env) == ai.sha256_64(CONV)
    ai.write_user_record(ai.sha256_64(SUB), {"brain_root": str(brain)})
    assert opened.caller_root(data, {"ANTIGRAVITY_CONVERSATION_ID": SUB}) == (
        ai.sha256_64(CONV)
    )
    assert opened.caller_root({**data, "conversationId": "nope"}, env) is None


def no_ancestry(*_args: object, **_kwargs: object) -> list[str]:
    return []


def test_opener_is_the_root_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ai, "ancestry", no_ancestry)
    assert opened.opener({}) is None
    env = {"CLAUDE_CODE_SESSION_ID": "session-raw", "CLAUDE_CODE_EXECPATH": "/x/2.1.0"}
    ident = opened.opener(env)
    assert ident == ai.Ident(ROOT, "claude-code:2.1.0")
    codex = opened.opener({"CODEX_SESSION_ID": "session-raw", "CODEX_THREAD_ID": "t"})
    assert codex is not None
    assert (codex.root, codex.subagent) == (ROOT, None)


def test_opener_in_antigravity_needs_a_resolved_root(tmp_path: Path) -> None:
    env = {"ANTIGRAVITY_CONVERSATION_ID": SUB}
    assert opened.opener(env) is None
    brain = make_brain(tmp_path, CONV)
    env["ANTIGRAVITY_APP_DATA_DIR"] = str(tmp_path)
    assert opened.opener(env) is None
    adopt(brain, CONV, SUB)
    ident = opened.opener(env)
    assert ident is not None
    assert (ident.root, ident.subagent) == (ai.sha256_64(CONV), None)
    assert opened.opener({**env, "ANTIGRAVITY_CONVERSATION_ID": CONV}) == ident


def test_opener_in_antigravity_finds_the_brain_in_its_record(tmp_path: Path) -> None:
    brain = make_brain(tmp_path / "elsewhere", CONV)
    adopt(brain, CONV, SUB)
    ai.write_user_record(ai.sha256_64(SUB), {"brain_root": str(brain)})
    ident = opened.opener({"ANTIGRAVITY_CONVERSATION_ID": SUB})
    assert ident is not None
    assert (ident.root, ident.subagent) == (ai.sha256_64(CONV), None)


def test_caller_root_of_antigravity_reads_the_worktree_record(tmp_path: Path) -> None:
    brain = make_brain(tmp_path / "elsewhere", CONV)
    adopt(brain, CONV, SUB)
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    sessions = repo / ".git" / ai.STORE / "sessions"
    sessions.mkdir(parents=True)
    record = sessions / f"{ai.hx(ai.sha256_64(SUB))}.json"
    record.write_text(json.dumps({"brain_root": str(brain)}))
    data: dict[str, object] = {"toolCall": {}, "conversationId": SUB}
    assert opened.caller_root(data, {}) is None
    data["cwd"] = str(repo)
    assert opened.caller_root(data, {}) == ai.sha256_64(CONV)


def test_a_non_utf8_ledger_raises(state: Path) -> None:
    state.mkdir(parents=True)
    path = state / f"{ai.hx(ROOT)}.json"
    path.write_bytes(b'{"prs": ["\xe9"]}')
    with pytest.raises(opened.LedgerError, match=f"^{path} is not UTF-8$"):
        opened.read_ledger(ROOT)

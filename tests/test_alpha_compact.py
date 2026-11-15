# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns
"""Tests for tbhbdev alpha compact, on a transcript and a home in a temporary dir."""

import io
import json
import os
import stat
import sys
import time
from pathlib import Path

import pytest

from tbhbdev.alpha import compact

type Json = dict[str, object]

HUMAN: Json = {"kind": "human"}
SECRET = "gh" + "p_" + "x" * 30
SESSION = "11111111-2222-3333-4444-555555555555"


@pytest.fixture(autouse=True)
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    return tmp_path / "home"


def user(text: object, stamp: str = "2026-11-15T01:02:03Z", **extra: object) -> Json:
    return {
        "type": "user",
        "timestamp": stamp,
        "origin": HUMAN,
        "message": {"content": text},
        **extra,
    }


def tool(name: str, **args: object) -> Json:
    block = {"type": "tool_use", "name": name, "input": args}
    return {
        "type": "assistant",
        "timestamp": "2026-11-15T02:03:04Z",
        "message": {"content": [block]},
    }


def write(path: Path, records: list[object]) -> Path:
    lines = [r if isinstance(r, str) else json.dumps(r) for r in records]
    path.write_text("\n".join(lines) + "\n")
    return path


def hook(transcript: Path, **extra: object) -> Json:
    return {
        "session_id": SESSION,
        "transcript_path": str(transcript),
        "trigger": "auto",
        **extra,
    }


def stdin(monkeypatch: pytest.MonkeyPatch, payload: object) -> None:
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))


def snapshots(home: Path) -> list[Path]:
    return sorted((home / compact.STATE / SESSION).iterdir())


# ------------------------------------------------------------------ scanning


def test_text_of() -> None:
    assert compact.text_of("plain") == "plain"
    blocks = [
        {"type": "text", "text": "a"},
        {"type": "image"},
        "x",
        {"type": "text", "text": "b"},
    ]
    assert compact.text_of(blocks) == "a\nb"
    assert compact.text_of([{"type": "text"}]) == ""
    assert compact.text_of(None) == ""


def test_is_human() -> None:
    assert compact.is_human(HUMAN)
    assert not compact.is_human({"kind": "agent"})
    assert not compact.is_human("human")


def test_scan_takes_human_messages_once_in_order(tmp_path: Path) -> None:
    path = write(
        tmp_path / "t.jsonl",
        [
            user("  first  "),
            user("first"),
            user("   "),
            user([{"type": "text", "text": "second"}], "2026-11-15T03:00:00Z"),
            user("meta", isMeta=True),
            user("summary", isCompactSummary=True),
            {"type": "user", "message": {"content": "not human"}},
            {"type": "user", "origin": HUMAN, "message": "odd"},
        ],
    )
    seg = compact.scan(path)
    assert seg.user == [
        ("2026-11-15T01:02:03Z", "first"),
        ("2026-11-15T03:00:00Z", "second"),
    ]


def test_scan_takes_queued_human_prompts(tmp_path: Path) -> None:
    queued = {
        "type": "queued_command",
        "origin": HUMAN,
        "prompt": "later",
        "timestamp": "T1",
    }
    other = {"type": "queued_command", "origin": {"kind": "task"}, "prompt": "no"}
    path = write(
        tmp_path / "t.jsonl",
        [
            {"type": "attachment", "attachment": queued, "timestamp": "T0"},
            {
                "type": "attachment",
                "attachment": {**queued, "timestamp": "", "prompt": "now"},
                "timestamp": "T2",
            },
            {"type": "attachment", "attachment": other},
            {
                "type": "attachment",
                "attachment": {"type": "hook_success", "origin": HUMAN},
            },
            {"type": "attachment", "attachment": "text"},
        ],
    )
    assert compact.scan(path).user == [("T1", "later"), ("T2", "now")]


def test_scan_takes_tool_calls(tmp_path: Path) -> None:
    path = write(
        tmp_path / "t.jsonl",
        [
            tool(
                "SendMessage",
                to="lead",
                summary="status",
                message="body that is not kept",
            ),
            tool("SendMessage"),
            tool("Write", file_path="/a"),
            tool("Edit", file_path="/b"),
            tool("NotebookEdit", notebook_path="/c.ipynb"),
            tool("Write"),
            tool("Agent", subagent_type="fork", model="m", description="d"),
            tool("Agent"),
            tool("Bash", command="echo kept out"),
            {"type": "assistant", "message": {"content": ["text", {"type": "text"}]}},
            {"type": "assistant", "message": {"content": "str"}},
            {"type": "assistant", "message": "odd"},
            {
                "type": "assistant",
                "message": {
                    "content": [{"type": "tool_use", "name": "Write", "input": "x"}]
                },
            },
        ],
    )
    seg = compact.scan(path)
    assert seg.sent == [
        ("2026-11-15T02:03:04Z", "lead", "status"),
        ("2026-11-15T02:03:04Z", "", ""),
    ]
    assert seg.files == {
        "/a": "2026-11-15T02:03:04Z",
        "/b": "2026-11-15T02:03:04Z",
        "/c.ipynb": "2026-11-15T02:03:04Z",
    }
    assert seg.agents == [
        ("2026-11-15T02:03:04Z", "fork", "m", "d"),
        ("2026-11-15T02:03:04Z", "", "", ""),
    ]
    assert "body that is not kept" not in repr(seg)
    assert "kept out" not in repr(seg)


def test_scan_starts_over_at_a_compact_boundary_and_skips_noise(tmp_path: Path) -> None:
    path = write(
        tmp_path / "t.jsonl",
        [
            user("before"),
            tool("Write", file_path="/old"),
            {
                "type": "system",
                "subtype": "compact_boundary",
                "timestamp": "2026-11-15T04:00:00Z",
            },
            {"type": "system", "subtype": "other"},
            user("after"),
            user("side", isSidechain=True),
            '{"type": "api-request-blob", "x": "user"}',
            "not json",
            "[1, 2]",
        ],
    )
    seg = compact.scan(path)
    assert seg.since == "2026-11-15T04:00:00Z"
    assert [t for _s, t in seg.user] == ["after"]
    assert seg.files == {}


def test_a_request_blob_is_skipped_by_its_prefix_only(tmp_path: Path) -> None:
    blob = json.dumps(
        {"type": "api-request-blob", "pad": "x" * 100, "tail": user("hidden")}
    )
    late = json.dumps(
        {
            "type": "user",
            "origin": HUMAN,
            "message": {"content": "m"},
            "pad": '"api-request',
        }
    )
    seg = compact.scan(write(tmp_path / "t.jsonl", [blob, late]))
    assert [t for _s, t in seg.user] == ["m"]


# ----------------------------------------------------------------- rendering


def test_hhmm() -> None:
    assert compact.hhmm("2026-11-15T01:02:03Z") == "01:02Z"
    assert compact.hhmm("2026-11-15T01:02") == "01:02Z"
    assert compact.hhmm("2026-11-15T01:0") == "?"
    assert compact.hhmm("") == "?"


def test_user_item_quotes_scrubs_and_cuts() -> None:
    item = compact.user_item("2026-11-15T01:02:03Z", "a\n\n" + SECRET, 100, "WHERE")
    assert item == "### 01:02Z\n\n> a\n>\n> [redacted]\n"
    cut = compact.user_item("2026-11-15T01:02:03Z", "abcdefghij", 4, "WHERE")
    assert cut == "### 01:02Z\n\n> abcd\n> [cut: 6 more characters in WHERE]\n"
    assert compact.user_item("", "abcd", 4, "W") == "### ?\n\n> abcd\n"


def segment() -> compact.Segment:
    seg = compact.Segment(since="2026-11-15T00:00:00Z")
    seg.user.append(("2026-11-15T01:02:03Z", "hello"))
    seg.sent.append(("2026-11-15T01:03:00Z", "lead", "status"))
    seg.sent.append(("2026-11-15T01:04:00Z", "peer", ""))
    seg.agents.append(("2026-11-15T01:05:00Z", "fork", "m", "build"))
    seg.agents.append(("2026-11-15T01:06:00Z", "", "", "x"))
    seg.files["/b"] = "2026-11-15T01:08:00Z"
    seg.files["/a"] = "2026-11-15T01:07:00Z"
    return seg


def test_sections_order_and_format() -> None:
    parts = compact.sections(segment(), 100, "W")
    assert [(k, t) for k, t, _i in parts] == [
        ("user", "The user's own messages, word for word"),
        ("sent", "Messages sent to other sessions (recipient and summary)"),
        ("agents", "Subagents started"),
        ("files", "Files written or edited"),
    ]
    items = {k: i for k, _t, i in parts}
    assert items["user"] == ["### 01:02Z\n\n> hello\n"]
    assert items["sent"] == [
        "- 01:03Z to **lead**: status",
        "- 01:04Z to **peer**: (no summary)",
    ]
    assert items["agents"] == [
        "- 01:05Z fork (model: m): build",
        "- 01:06Z default (model: NOT SET): x",
    ]
    assert items["files"] == ["- 01:07Z `/a`", "- 01:08Z `/b`"]


def test_sections_scrub_every_field() -> None:
    secret = "ghp_" + "z" * 30
    seg = compact.Segment()
    seg.sent.append(("", secret, secret))
    seg.agents.append(("", secret, secret, secret))
    seg.files[secret] = ""
    text = repr(compact.sections(seg, 100, "W"))
    assert "zzz" not in text


def test_render_full_holds_everything() -> None:
    seg = segment()
    seg.user = [("2026-11-15T01:02:03Z", "x" * 4001)]
    text = compact.render_full(seg, SESSION, "auto")
    assert text.startswith(f"# Pre-compaction snapshot, session {SESSION}\n")
    assert "(auto)" in text
    assert "since 2026-11-15T00:00:00Z" in text
    assert "[cut: 1 more characters in the transcript]" in text
    assert "## The user's own messages, word for word (1)" in text
    assert "## Files written or edited (2)" in text
    empty = compact.render_full(compact.Segment(), SESSION, "manual")
    assert "since the session start" in empty
    assert empty.count("- none") == 4


def test_take_keeps_the_newest_that_fit() -> None:
    assert compact.take(["aaaa", "bbbb", "cccc"], 10) == (["bbbb", "cccc"], 1)
    assert compact.take(["aaaa", "bbbb"], 5) == (["bbbb"], 1)
    assert compact.take(["aaaa", "bbbb"], 4) == ([], 2)
    assert compact.take([], 5) == ([], 0)
    assert compact.take(["a", "b"], 100) == (["a", "b"], 0)


def test_render_digest_points_to_full_and_marks_what_it_left_out(
    tmp_path: Path,
) -> None:
    seg = compact.Segment()
    seg.user = [("2026-11-15T01:00:00Z", f"m{i} " + "y" * 600) for i in range(20)]
    full = tmp_path / "full.md"
    text = compact.render_digest(seg, SESSION, "auto", full)
    assert text.startswith(f"PRE-COMPACTION SNAPSHOT for session {SESSION}.")
    assert f"Everything is in {full}." in text
    assert "m19 " in text
    assert "m0 " not in text
    assert f"older left out; read {full}]" in text
    assert "## Files written or edited (0)\n\n- none\n" in text
    assert len(text) <= compact.LIMIT


def test_a_digest_stays_under_the_cap_when_every_section_is_full(
    tmp_path: Path,
) -> None:
    seg = compact.Segment()
    seg.user = [("2026-11-15T01:00:00Z", "u" * 5000) for _ in range(30)]
    seg.sent = [("2026-11-15T01:00:00Z", "to", "s" * 200)] * 60
    seg.agents = [("2026-11-15T01:00:00Z", "t", "m", "d" * 100)] * 30
    seg.files = {f"/path/{'p' * 100}{i}": "2026-11-15T01:00:00Z" for i in range(40)}
    text = compact.render_digest(seg, SESSION, "auto", tmp_path / "full.md")
    assert len(text) < compact.LIMIT


def test_fit(tmp_path: Path) -> None:
    full = tmp_path / "full.md"
    assert compact.fit("x" * compact.LIMIT, full) == "x" * compact.LIMIT
    cut = compact.fit("x" * (compact.LIMIT + 1), full)
    assert len(cut) == compact.LIMIT
    assert cut.endswith(f"\n[cut here; read {full}]")


def test_safe_session() -> None:
    assert compact.safe_session(SESSION) == SESSION
    for bad in (None, 7, "", ".", "..", "../x", "a/b", ".hidden", "a" * 129):
        assert compact.safe_session(bad) == "unknown"
    assert compact.safe_session("a" * 128) == "a" * 128


# --------------------------------------------------------------- save, prune


def test_save_writes_private_files_and_returns_the_digest(
    tmp_path: Path, home: Path
) -> None:
    path = write(tmp_path / "t.jsonl", [user("hello ghp_" + "q" * 30)])
    digest = compact.save(hook(path))
    (folder,) = snapshots(home)
    assert digest == folder / "digest.md"
    for name in ("full.md", "digest.md"):
        file = folder / name
        assert file.read_text().count("hello [redacted]") == 1
        assert stat.S_IMODE(file.stat().st_mode) == 0o600
    assert not list(folder.glob("*.tmp"))
    for directory in (home / compact.STATE, home / compact.STATE / SESSION, folder):
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    assert "Everything is in " + str(folder / "full.md") in digest.read_text()
    assert len(folder.name) == len("20261115T010203Z")


@pytest.mark.parametrize("transcript", [None, 3, "/nonexistent/x.jsonl"])
def test_save_needs_a_transcript(transcript: object) -> None:
    with pytest.raises(RuntimeError, match="no transcript at"):
        compact.save({"session_id": SESSION, "transcript_path": transcript})


def test_save_uses_unknown_for_an_unsafe_session_and_trigger(
    tmp_path: Path, home: Path
) -> None:
    path = write(tmp_path / "t.jsonl", [user("hi")])
    compact.save(
        {"session_id": "../../escape", "transcript_path": str(path), "trigger": ""}
    )
    (folder,) = (home / compact.STATE / "unknown").iterdir()
    assert "(unknown)" in (folder / "full.md").read_text()
    assert not (home / "escape").exists()


def test_prune_keeps_the_newest_snapshots_of_a_session(home: Path) -> None:
    root = home / compact.STATE
    current = root / SESSION
    current.mkdir(parents=True)
    names = [f"2026{n:04d}T000000Z" for n in range(compact.KEEP_SNAPSHOTS + 3)]
    for name in names:
        (current / name).mkdir()
    (current / "stray.txt").write_text("kept")
    compact.prune(root, current, time.time())
    left = sorted(p.name for p in current.iterdir() if p.is_dir())
    assert left == names[3:]
    assert (current / "stray.txt").exists()
    assert len(left) == compact.KEEP_SNAPSHOTS


def test_prune_drops_idle_sessions_only(home: Path) -> None:
    root = home / compact.STATE
    current, idle, fresh = root / SESSION, root / "idle", root / "fresh"
    for folder in (current, idle, fresh):
        (folder / "20260101T000000Z").mkdir(parents=True)
    (root / "note.txt").write_text("kept")
    now = time.time()
    day = 86400
    os.utime(idle, (now - 31 * day, now - 31 * day))
    os.utime(fresh, (now - 29 * day, now - 29 * day))
    os.utime(current, (now - 90 * day, now - 90 * day))
    compact.prune(root, current, now)
    assert not idle.exists()
    assert fresh.exists()
    assert current.exists()
    assert (root / "note.txt").exists()


def test_write_private_replaces_a_stale_temp_file(tmp_path: Path) -> None:
    target = tmp_path / "out.md"
    (tmp_path / "out.md.tmp").write_text("stale and much longer than the new text")
    compact.write_private(target, "new")
    assert target.read_text() == "new"
    assert not (tmp_path / "out.md.tmp").exists()


# ------------------------------------------------------------------ the hooks


def test_save_mode_prints_what_to_keep_and_the_snapshot_path(
    tmp_path: Path,
    home: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = write(tmp_path / "t.jsonl", [user("hi")])
    stdin(monkeypatch, hook(path))
    assert compact.main(["save"], "compact") == 0
    out, err = capsys.readouterr()
    (folder,) = snapshots(home)
    assert out.startswith(compact.PRESERVE)
    assert (
        f"saved word for word in {folder / 'full.md'}; name that file in the summary."
        in out
    )
    assert err == ""
    assert len(out) < 1000


def test_save_mode_still_prints_what_to_keep_when_the_save_fails(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    stdin(monkeypatch, {"session_id": SESSION})
    assert compact.main(["save"], "compact") == 0
    out, err = capsys.readouterr()
    assert out == compact.PRESERVE + "\n"
    assert err.startswith("compact save: RuntimeError: no transcript at None")


def test_restore_prints_the_newest_digest(
    tmp_path: Path,
    home: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = write(tmp_path / "t.jsonl", [user("old message")])
    compact.save(hook(path))
    newer = home / compact.STATE / SESSION / "99990101T000000Z"
    newer.mkdir()
    (newer / "digest.md").write_text("NEWEST DIGEST")
    unfinished = home / compact.STATE / SESSION / "99990202T000000Z"
    unfinished.mkdir()
    (unfinished / "full.md").write_text("no digest yet")
    stdin(monkeypatch, {"session_id": SESSION})
    assert compact.main(["restore"], "compact") == 0
    assert capsys.readouterr() == ("NEWEST DIGEST\n", "")


def test_restore_falls_back_past_a_folder_without_a_digest(
    tmp_path: Path,
    home: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = write(tmp_path / "t.jsonl", [user("kept message")])
    compact.save(hook(path))
    unfinished = home / compact.STATE / SESSION / "99990202T000000Z"
    unfinished.mkdir()
    stdin(monkeypatch, {"session_id": SESSION})
    compact.main(["restore"], "compact")
    assert "kept message" in capsys.readouterr().out


def test_restore_cuts_an_oversized_digest(
    home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    folder = home / compact.STATE / SESSION / "20260101T000000Z"
    folder.mkdir(parents=True)
    (folder / "digest.md").write_text("x" * 20000)
    stdin(monkeypatch, {"session_id": SESSION})
    compact.main(["restore"], "compact")
    out = capsys.readouterr().out
    assert len(out.rstrip("\n")) == compact.LIMIT
    assert out.rstrip("\n").endswith(f"[cut here; read {folder / 'full.md'}]")


def test_restore_prints_nothing_without_a_snapshot(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], home: Path
) -> None:
    stdin(monkeypatch, {"session_id": SESSION})
    assert compact.main(["restore"], "compact") == 0
    stdin(monkeypatch, {})
    assert compact.main(["restore"], "compact") == 0
    (home / compact.STATE / "unknown").mkdir(parents=True)
    stdin(monkeypatch, "not an object")
    assert compact.main(["restore"], "compact") == 0
    assert capsys.readouterr() == ("", "")


def test_a_failing_restore_exits_0_with_a_note(
    home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    folder = home / compact.STATE / SESSION / "20260101T000000Z"
    folder.mkdir(parents=True)
    (folder / "digest.md").write_bytes(b"x")

    def unreadable(_path: Path, *, errors: str) -> str:  # noqa: ARG001
        message = "boom"
        raise OSError(message)

    monkeypatch.setattr(Path, "read_text", unreadable)
    stdin(monkeypatch, {"session_id": SESSION})
    assert compact.main(["restore"], "compact") == 0
    assert capsys.readouterr() == ("", "compact restore: OSError: boom\n")


@pytest.mark.parametrize("argv", [[], ["other"], ["save", "x"]])
def test_a_usage_error_exits_1_never_2(
    argv: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    assert compact.main(argv, "tbhbdev alpha compact") == 1
    assert capsys.readouterr() == ("", "usage: tbhbdev alpha compact save|restore\n")


@pytest.mark.usefixtures("home")
def test_the_hook_runs_from_the_top(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from tbhbdev import cli  # noqa: PLC0415 -- the entry point the installed hook runs

    path = write(tmp_path / "t.jsonl", [user("from the top")])
    stdin(monkeypatch, hook(path))
    assert cli.main(["alpha", "compact", "save"]) == 0
    capsys.readouterr()
    stdin(monkeypatch, {"session_id": SESSION})
    assert cli.main(["alpha", "compact", "restore"]) == 0
    assert "from the top" in capsys.readouterr().out

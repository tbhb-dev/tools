# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns
"""`tbhbdev alpha compact`: save what a compaction loses and hand it back.

Two modes, both Claude Code command hooks that read the hook payload on stdin:

- `save` (PreCompact) scans the transcript since the last compaction and writes
  a snapshot under `~/.local/state/tbhbdev/compact/<session>/<time>/`: `full.md`
  holds everything and `digest.md` holds what fits the harness's hook output
  cap. It then prints what the compaction summary must keep, which Claude Code
  passes to the summarizing model as instructions.
- `restore` (SessionStart, matcher `compact`) prints the newest digest for the
  session so it lands in the new context.

Claude Code caps a hook's output at 10,000 characters and, above that, passes
on only a 2,000-character preview. The digest stays under 9,900 and names
`full.md` for the rest. The snapshot is copied from the transcript, not
summarized: the user's own messages word for word, the recipient and summary of
every message sent to another session, subagents started, and files written.
Every text passes through `tbhbdev.alpha.scrub` first.

A snapshot folder past the newest 20 of its session goes, and so does a whole
session folder that no save has touched for 30 days. The hook never fails a
compaction or a session start: any error exits 0 with a note on stderr.
"""

import json
import os
import re
import shutil
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

from tbhbdev.alpha.repo import read_hook_stdin
from tbhbdev.alpha.scrub import scrub

type Json = dict[str, object]

STATE = Path(".local/state/tbhbdev/compact")  # relative to the home directory
KEEP_SNAPSHOTS = 20  # per session
KEEP_DAYS = 30  # for a session no save has touched
USER_CHARS_FULL = 4000  # per user message in full.md
USER_CHARS_DIGEST = 700  # per user message in digest.md
LIMIT = 9900  # characters printed by restore; the harness cap is 10,000
BUDGETS = {"user": 5000, "sent": 2000, "agents": 700, "files": 800}
SESSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
CLOCK_END = 16  # where `HH:MM` ends in an ISO timestamp
EDITS = ("Write", "Edit", "MultiEdit", "NotebookEdit")

PRESERVE = (
    "When you write the compaction summary, keep these in exact words and never "
    "paraphrase them: every review verdict or finding received, with its pull "
    "request number and head sha; the full command of each peer run launched; "
    "cron job IDs; awrel subscription filters; background shell, monitor and "
    "agent IDs; and each commitment you owe, marked done or not done."
)


@dataclass(slots=True)
class Segment:
    """What one stretch of transcript, after the last compaction, holds."""

    since: str = ""
    user: list[tuple[str, str]] = field(default_factory=list)
    seen: set[str] = field(default_factory=set)
    sent: list[tuple[str, str, str]] = field(default_factory=list)
    agents: list[tuple[str, str, str, str]] = field(default_factory=list)
    files: dict[str, str] = field(default_factory=dict)


def objects(items: object) -> list[Json]:
    """The JSON objects among ITEMS, which is a list or something else."""
    if not isinstance(items, list):
        return []
    listed = cast("list[object]", items)  # pragma: no mutate
    return [cast("Json", item) for item in listed if isinstance(item, dict)]


def text_of(content: object) -> str:
    """The text of a message's CONTENT: a string, or its text blocks joined."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        blocks = objects(content)
        return "\n".join(
            str(b.get("text", "")) for b in blocks if b.get("type") == "text"
        )
    return ""


def is_human(origin: object) -> bool:
    """Whether a record's ORIGIN says a person typed it."""
    return isinstance(origin, dict) and origin.get("kind") == "human"


def add_user(seg: Segment, stamp: str, text: str) -> None:
    """Record a user message once; blank and repeated ones are skipped."""
    text = text.strip()
    if not text or text in seg.seen:
        return
    seg.seen.add(text)
    seg.user.append((stamp, text))


def on_user(seg: Segment, rec: Json, stamp: str) -> None:
    """Take a human-typed user record."""
    if rec.get("isMeta") or rec.get("isCompactSummary"):
        return
    message = rec.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if is_human(rec.get("origin")):
        add_user(seg, stamp, text_of(content))


def on_attachment(seg: Segment, rec: Json, stamp: str) -> None:
    """Take a queued prompt a person typed while the model was working."""
    att = rec.get("attachment")
    if not isinstance(att, dict):
        return
    if att.get("type") == "queued_command" and is_human(att.get("origin")):
        add_user(seg, str(att.get("timestamp") or stamp), text_of(att.get("prompt")))


def on_assistant(seg: Segment, rec: Json, stamp: str) -> None:
    """Take the tool calls an assistant record holds."""
    message = rec.get("message")
    blocks = message.get("content") if isinstance(message, dict) else None
    for block in objects(blocks):
        if block.get("type") != "tool_use":
            continue
        name, raw = block.get("name"), block.get("input")
        args: Json = raw if isinstance(raw, dict) else {}
        if name == "SendMessage":
            seg.sent.append(
                (stamp, str(args.get("to", "")), str(args.get("summary", "")))
            )
        elif name in EDITS:
            path = args.get("file_path") or args.get("notebook_path")
            if path:
                seg.files[str(path)] = stamp
        elif name == "Agent":
            seg.agents.append(
                (
                    stamp,
                    str(args.get("subagent_type", "")),
                    str(args.get("model", "")),
                    str(args.get("description", "")),
                )
            )


def scan(path: Path) -> Segment:
    """The records of the transcript at PATH after its last compact boundary."""
    seg = Segment()
    with path.open(errors="replace") as lines:
        for line in lines:
            if '"api-request' in line[:80]:  # request blobs hold nothing we keep
                continue
            try:
                loaded = json.loads(line)
            except ValueError:
                continue
            if not isinstance(loaded, dict):
                continue
            rec = cast("Json", loaded)  # pragma: no mutate
            kind = rec.get("type")
            stamp = str(rec.get("timestamp", ""))
            if kind == "system" and rec.get("subtype") == "compact_boundary":
                seg = Segment(since=stamp)
            elif rec.get("isSidechain"):
                continue
            elif kind == "user":
                on_user(seg, rec, stamp)
            elif kind == "attachment":
                on_attachment(seg, rec, stamp)
            elif kind == "assistant":
                on_assistant(seg, rec, stamp)
    return seg


def hhmm(stamp: str) -> str:
    """The UTC clock time of an ISO STAMP, or `?`."""
    return stamp[11:CLOCK_END] + "Z" if len(stamp) >= CLOCK_END else "?"


def user_item(stamp: str, text: str, limit: int, where: str) -> str:
    """One user message as a quoted block, cut to LIMIT characters."""
    text = scrub(text)
    if len(text) > limit:
        text = text[:limit] + f"\n[cut: {len(text) - limit} more characters in {where}]"
    quoted = "\n".join("> " + ln if ln else ">" for ln in text.splitlines())
    return f"### {hhmm(stamp)}\n\n{quoted}\n"


def sections(seg: Segment, limit: int, where: str) -> list[tuple[str, str, list[str]]]:
    """The segment as (key, title, items), most important first."""
    user = [user_item(s, t, limit, where) for s, t in seg.user]
    sent = [
        f"- {hhmm(s)} to **{scrub(to)}**: {scrub(text) or '(no summary)'}"
        for s, to, text in seg.sent
    ]
    agents = [
        f"- {hhmm(s)} {scrub(kind) or 'default'} "
        f"(model: {scrub(model) or 'NOT SET'}): {scrub(desc)}"
        for s, kind, model, desc in seg.agents
    ]
    files = [
        f"- {hhmm(s)} `{scrub(p)}`"
        for p, s in sorted(seg.files.items(), key=lambda kv: kv[1])
    ]
    return [
        ("user", "The user's own messages, word for word", user),
        ("sent", "Messages sent to other sessions (recipient and summary)", sent),
        ("agents", "Subagents started", agents),
        ("files", "Files written or edited", files),
    ]


def header(session: str, trigger: str, seg: Segment) -> list[str]:
    """The lines that open a snapshot."""
    return [
        f"# Pre-compaction snapshot, session {session}",
        "",
        (
            f"Written {time.strftime('%Y-%m-%d %H:%M:%SZ', time.gmtime())} "
            f"by the PreCompact hook ({trigger})."
        ),
        f"It covers the transcript since {seg.since or 'the session start'}.",
        "Every line is copied from the transcript, not summarized. Times are UTC.",
        "",
    ]


def render_full(seg: Segment, session: str, trigger: str) -> str:
    """full.md: every item of every section."""
    out = header(session, trigger, seg)
    for _key, title, items in sections(seg, USER_CHARS_FULL, "the transcript"):
        out += [f"## {title} ({len(items)})", "", *(items or ["- none"]), ""]
    return "\n".join(out)


def take(items: list[str], budget: int) -> tuple[list[str], int]:
    """The newest ITEMS that fit BUDGET characters, in order, and the count left out."""
    kept: list[str] = []
    used = 0
    for item in reversed(items):
        used += len(item) + 1
        if used > budget:
            break
        kept.append(item)
    kept.reverse()
    return kept, len(items) - len(kept)


def render_digest(seg: Segment, session: str, trigger: str, full: Path) -> str:
    """digest.md: the newest items of each section, within a budget each."""
    out = [
        (
            f"PRE-COMPACTION SNAPSHOT for session {session}. It is exact where the "
            "summary paraphrases; trust it over the summary."
        ),
        f"Everything is in {full}.",
        "",
        *header(session, trigger, seg)[2:],
    ]
    for key, title, items in sections(seg, USER_CHARS_DIGEST, str(full)):
        kept, left = take(items, BUDGETS[key])
        out += [f"## {title} ({len(items)})", ""]
        if left:
            out.append(f"[{left} older left out; read {full}]")
        out += [*(kept or ["- none"]), ""]
    return "\n".join(out)


def fit(text: str, full: Path) -> str:
    """TEXT, cut to LIMIT characters with a pointer when it is longer."""
    if len(text) <= LIMIT:
        return text
    note = f"\n[cut here; read {full}]"
    return text[: LIMIT - len(note)] + note


def safe_session(value: object) -> str:
    """The session id as a folder name; `unknown` for anything not plain."""
    return value if isinstance(value, str) and SESSION.fullmatch(value) else "unknown"


def private_dir(path: Path) -> None:
    """Make the folder PATH, readable by the user only."""
    path.mkdir(mode=0o700, exist_ok=True)


def write_private(path: Path, text: str) -> None:
    """Write TEXT to PATH, readable by the user only, all at once."""
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write(text)
    tmp.replace(path)


def prune(root: Path, current: Path, now: float) -> None:
    """Drop the CURRENT session's oldest snapshots and other sessions idle too long."""
    for old in sorted(p for p in current.iterdir() if p.is_dir())[:-KEEP_SNAPSHOTS]:
        shutil.rmtree(old, ignore_errors=True)
    for other in root.iterdir():
        if (
            other != current
            and other.is_dir()
            and now - other.stat().st_mtime > KEEP_DAYS * 86400
        ):
            shutil.rmtree(other, ignore_errors=True)


def save(hook: Json) -> Path:
    """Write the snapshot for the hook payload HOOK and return its digest path."""
    transcript = hook.get("transcript_path")
    if not isinstance(transcript, str) or not Path(transcript).is_file():
        message = f"no transcript at {transcript!r}"
        raise RuntimeError(message)
    seg = scan(Path(transcript))
    session = safe_session(hook.get("session_id"))
    trigger = str(hook.get("trigger") or "unknown")
    root = Path.home() / STATE
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    current = root / session
    private_dir(current)
    folder = current / time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    private_dir(folder)
    write_private(folder / "full.md", render_full(seg, session, trigger))
    digest = folder / "digest.md"
    # The digest goes last: restore reads only a folder that holds one.
    write_private(
        digest,
        fit(
            render_digest(seg, session, trigger, folder / "full.md"), folder / "full.md"
        ),
    )
    prune(root, current, time.time())
    return digest


def run_save(hook: Json) -> None:
    """PreCompact: save, then print what the summary must keep."""
    note = PRESERVE
    try:
        digest = save(hook)
    except Exception as exc:  # noqa: BLE001  # a broken hook must not stop a compaction
        print(f"compact save: {type(exc).__name__}: {exc}", file=sys.stderr)
    else:
        full = digest.parent / "full.md"
        note += (
            f" The user's own messages are saved word for word in {full}; "
            "name that file in the summary."
        )
    print(note)


def run_restore(hook: Json) -> None:
    """SessionStart on compact: print the newest digest of the session."""
    folder = Path.home() / STATE / safe_session(hook.get("session_id"))
    if not folder.is_dir():
        return
    for snapshot in sorted((p for p in folder.iterdir() if p.is_dir()), reverse=True):
        digest = snapshot / "digest.md"
        if digest.is_file():
            print(fit(digest.read_text(errors="replace"), snapshot / "full.md"))
            return


MODES = {"save": run_save, "restore": run_restore}


def main(argv: list[str], prog: str) -> int:
    """Run `save` or `restore`; exit 0 unless the mode is unknown.

    A hook that exits 2 blocks the event, so a usage error exits 1.
    """
    if len(argv) != 1 or argv[0] not in MODES:
        sys.stderr.write(f"usage: {prog} save|restore\n")
        return 1
    try:
        MODES[argv[0]](read_hook_stdin())
    except Exception as exc:  # noqa: BLE001  # a broken hook must not stop a session
        print(f"compact {argv[0]}: {type(exc).__name__}: {exc}", file=sys.stderr)
    return 0

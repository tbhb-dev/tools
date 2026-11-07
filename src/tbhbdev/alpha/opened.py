# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns
"""The opened ledger: which pull requests each root session opened.

A session can't approve a pull request that it, or one of its subagents,
opened. Who a session is, for that rule, is its root session hash, the
``session=`` value of its trailers. A Claude Code subagent's hook payload
carries its parent's ``session_id``, a Codex hook's ``session_id`` is the root
session's, and an Antigravity child resolves to its root through the parent
links in its brain directory.

``tbhbdev alpha pr create`` adds each pull request it opens to the posting
root's ledger, ``<user store>/opened/<root hex>.json``, as ``{"prs": [...]}``:
sorted, deduplicated ``owner/repo#N`` entries, written whole under a lock.
``tbhbdev alpha pr backfill-opened`` adds the pull requests opened before
the ledger existed. The guard reads the ledger with no network call.
"""

import json
from pathlib import Path
from typing import TYPE_CHECKING, cast

from tbhbdev.alpha.identity import (
    ANTIGRAVITY,
    Json,
    agy_brain_root,
    agy_root,
    as_str,
    committer_identity,
    hx,
    load_record,
    locked,
    parse_stdin,
    sha256_64,
    user_store,
    write_private_json,
)
from tbhbdev.alpha.repo import hook_cwd

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

    from tbhbdev.alpha.identity import Ident

LEDGER = "opened"
AGY_ENV = "ANTIGRAVITY_CONVERSATION_ID"


class LedgerError(Exception):
    """A ledger file that exists but can't be read as a ledger."""


def ledger_path(root: str) -> Path:
    """The ledger file of a root session hash."""
    return user_store() / LEDGER / f"{hx(root)}.json"


def read_ledger(root: str) -> set[str]:
    """The pull requests a root session opened; empty when it has no ledger.

    Raises:
        LedgerError: The file exists but can't be read, or isn't a ledger.
    """
    path = ledger_path(root)
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return set()
    except OSError as exc:
        message = f"cannot read {path}: {exc.strerror}"
        raise LedgerError(message) from exc
    try:
        data: object = json.loads(raw)
    except UnicodeDecodeError as exc:
        message = f"{path} is not UTF-8"
        raise LedgerError(message) from exc
    except ValueError as exc:
        message = f"{path} is not JSON"
        raise LedgerError(message) from exc
    prs = data.get("prs") if isinstance(data, dict) else None
    refs: list[object] = [None]
    if isinstance(prs, list):
        refs = cast("list[object]", prs)  # pragma: no mutate -- a runtime no-op
    if not all(isinstance(ref, str) for ref in refs):
        message = f"{path} holds no list of pull requests"
        raise LedgerError(message)
    return {str(ref) for ref in refs}


def record_opened(root: str, refs: Iterable[str]) -> None:
    """Add ``owner/repo#N`` entries to a root session's ledger.

    Raises:
        LedgerError: The ledger exists but isn't one; it is left as it is.
    """
    with locked(user_store() / LEDGER):
        known = read_ledger(root)
        write_private_json(ledger_path(root), {"prs": sorted(known.union(refs))})


def agy_root_hash(cid: str, brain: Path | None) -> str | None:
    """An Antigravity conversation's root hash, or None when it can't be known.

    Only a brain directory that holds the conversation can show whether it is
    a child, so without one the root is unknown, never the child's own hash.
    """
    if brain is None or not (brain / cid).is_dir():
        return None
    return sha256_64(agy_root(brain, cid))


def opener(env: Mapping[str, str]) -> Ident | None:
    """The root identity of the agent opening a pull request, or None.

    None outside a harness, and for an Antigravity conversation whose root
    can't be resolved.
    """
    committer = committer_identity(env)
    if committer is None:
        return None
    if committer.key == ANTIGRAVITY:
        cid = env[AGY_ENV]  # detect() found the conversation here
        brain = agy_brain_root(load_record(sha256_64(cid)), env)
        if agy_root_hash(cid, brain) is None:
            return None
    return committer.root_ident


def caller_root(data: Json, env: Mapping[str, str]) -> str | None:
    """The root session hash behind a PreToolUse payload, or None.

    Claude Code and Codex send the root's ``session_id``. Antigravity sends
    the executing conversation, whose brain directory is the parent of its
    ``artifactDirectoryPath``.
    """
    sid = parse_stdin(data)[0]
    if "toolCall" not in data:
        return sha256_64(sid) if sid else None
    cid = sid or env.get(AGY_ENV)
    if not cid:
        return None
    artifacts = as_str(data.get("artifactDirectoryPath"))
    if artifacts:
        brain: Path | None = Path(artifacts).parent
    else:
        brain = agy_brain_root(load_record(sha256_64(cid), hook_cwd(data)), env)
    return agy_root_hash(cid, brain)

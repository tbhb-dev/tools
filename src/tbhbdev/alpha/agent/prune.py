"""Inventory this coordinator's runs and move selected runs through clean."""

from __future__ import annotations

import re
import sys
import time
from typing import TYPE_CHECKING

from tbhbdev.alpha.agent import driver, transport
from tbhbdev.alpha.agent.decisions import driver as rules
from tbhbdev.alpha.agent.decisions import prune as selection
from tbhbdev.alpha.agent.decisions import prune_remote
from tbhbdev.alpha.agent.model import Invalid

if TYPE_CHECKING:
    import argparse
    from collections.abc import Iterator

    from tbhbdev.alpha.agent.model import Settings


def names() -> Iterator[str]:
    for path in sorted(driver.directory().glob("*.json")):
        if path.is_symlink() or not path.is_file():
            continue
        try:
            rules.check_name(path.stem)
        except Invalid:
            continue
        yield path.stem


def read(settings: Settings, run: driver.Run) -> selection.Facts:
    text = transport.shell(
        settings,
        run.host,
        prune_remote.facts(
            run.request.repo, run.name, settings.sockets_dir, owner=run.owner
        ),
    )
    match = re.fullmatch(r"([01])\n(-|[0-9]+)\n([01])\n([0-9]+)\n([0-9]+)\n", text)
    if match is None:
        message = "invalid prune facts"
        raise Invalid(message)
    session, mtime, keep, files, size = match.groups()
    return selection.Facts(
        session == "1",
        None if mtime == "-" else int(mtime),
        keep == "1",
        int(files),
        int(size),
    )


def failed(run: driver.Run, exc: Exception) -> None:
    print(
        transport.terminal_text(f"prune failed: {run.host.name} {run.name}: {exc}"),
        file=sys.stderr,
    )


def remove(
    settings: Settings, run: driver.Run, args: argparse.Namespace
) -> selection.Facts | None:
    facts = read(settings, run)
    if (
        selection.decide(facts, now=time.time(), days=args.older_than, force=args.force)
        != "prune"
    ):
        print(
            transport.terminal_text(
                f"prune changed, skipped: {run.host.name} {run.name}"
            ),
            file=sys.stderr,
        )
        return None
    driver.clean(settings, run, force=False)
    return facts


def main(settings: Settings, args: argparse.Namespace) -> int:
    status = 0
    gathered: list[tuple[driver.Run, selection.Facts]] = []
    for name in names():
        try:
            run = driver.load(settings, name)
        except Invalid:
            print(
                transport.terminal_text(f"prune: invalid metadata: {name}"),
                file=sys.stderr,
            )
            status = 1
            continue
        try:
            gathered.append((run, read(settings, run)))
        except (Invalid, transport.TransportError, transport.CollisionError) as exc:
            failed(run, exc)
            status = 1
    if args.largest is not None:
        ranked = sorted(gathered, key=lambda pair: (-pair[1].bytes, pair[0].name))
        for run, facts in ranked[: args.largest]:
            print(
                transport.terminal_text(
                    f"largest: {run.host.name} {run.name} "
                    f"bytes={facts.bytes} files={facts.files}"
                )
            )
        return status
    totals: dict[str, tuple[int, int, int]] = {}
    for run, facts in gathered:
        reason = selection.decide(
            facts, now=time.time(), days=args.older_than, force=args.force
        )
        if reason == "recent":
            continue
        label = f"{run.host.name} {run.name}"
        if reason != "prune":
            print(transport.terminal_text(f"{reason}: {label}"))
            continue
        print(
            transport.terminal_text(
                f"prune: {label} bytes={facts.bytes} files={facts.files}"
            )
        )
        runs, files, size = totals.setdefault(run.host.name, (0, 0, 0))
        measured = facts
        if args.apply:
            try:
                current = remove(settings, run, args)
            except (Invalid, transport.TransportError, transport.CollisionError) as exc:
                failed(run, exc)
                status = 1
                continue
            if current is None:
                status = 1
                continue
            measured = current
        totals[run.host.name] = (
            runs + 1,
            files + measured.files,
            size + measured.bytes,
        )
    action = "removed" if args.apply else "would free"
    for host, (runs, files, size) in sorted(totals.items()):
        print(
            transport.terminal_text(
                f"{action}: {host} runs={runs} files={files} bytes={size}"
            )
        )
    return status

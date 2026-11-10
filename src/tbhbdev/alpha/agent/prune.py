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
    measure = not run.request.unsandboxed
    text = transport.shell(
        settings,
        run.host,
        prune_remote.facts(
            run.request.repo,
            run.name,
            settings.sockets_dir,
            owner=run.owner,
            measure=measure,
        ),
    )
    counts = r"([0-9]+)\n([0-9]+)\n" if measure else r"(-)\n(-)\n"
    match = re.fullmatch(r"([01])\n(-|[0-9]+)\n([01])\n" + counts, text)
    if match is None:
        message = "invalid prune facts"
        raise Invalid(message)
    session, mtime, keep, files, size = match.groups()
    return selection.Facts(
        session == "1",
        None if mtime == "-" else int(mtime),
        keep == "1",
        # Unmeasured placeholders never enter rankings or totals.
        int(files) if measure else 0,
        int(size) if measure else 0,
    )


def failed(run: driver.Run, exc: Exception) -> None:
    print(
        transport.terminal_text(f"prune failed: {run.host.name} {run.name}: {exc}"),
        file=sys.stderr,
    )


def remove(
    settings: Settings, run: driver.Run, args: argparse.Namespace
) -> selection.Facts | int:
    facts = read(settings, run)
    reason = selection.decide(
        facts,
        now=time.time(),
        days=args.older_than,
        force=args.force,
        unsandboxed=run.request.unsandboxed,
    )
    if reason == "unsandboxed":
        skipped(run, reason)
        return 0
    if reason != "prune":
        print(
            transport.terminal_text(
                f"prune changed, skipped: {run.host.name} {run.name}"
            ),
            file=sys.stderr,
        )
        return 1
    driver.clean(settings, run, force=False)
    return facts


def skipped(run: driver.Run, reason: str) -> None:
    suffix = (
        " (not measured; clean with agent clean --force)"
        if reason == "unsandboxed"
        else ""
    )
    print(transport.terminal_text(f"{reason}: {run.host.name} {run.name}{suffix}"))


def largest(gathered: list[tuple[driver.Run, selection.Facts]], limit: int) -> None:
    ranked = sorted(
        (pair for pair in gathered if not pair[0].request.unsandboxed),
        key=lambda pair: (-pair[1].bytes, pair[0].name),
    )
    for run, facts in ranked[:limit]:
        print(
            transport.terminal_text(
                f"largest: {run.host.name} {run.name} "
                f"bytes={facts.bytes} files={facts.files}"
            )
        )
    for run, _facts in sorted(gathered, key=lambda pair: pair[0].name):
        if run.request.unsandboxed:
            print(
                transport.terminal_text(
                    f"unsandboxed: {run.host.name} {run.name} (not measured)"
                )
            )


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
        largest(gathered, args.largest)
        return status
    totals: dict[str, tuple[int, int, int]] = {}
    for run, facts in gathered:
        reason = selection.decide(
            facts,
            now=time.time(),
            days=args.older_than,
            force=args.force,
            unsandboxed=run.request.unsandboxed,
        )
        if reason == "recent":
            continue
        label = f"{run.host.name} {run.name}"
        if reason != "prune":
            skipped(run, reason)
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
            if isinstance(current, int):
                status |= current
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

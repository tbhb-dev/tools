"""Attach ssh to the caller's terminal, without capturing any of its streams."""

from __future__ import annotations

import subprocess  # nosec B404
import sys
from typing import TYPE_CHECKING

from tbhbdev.alpha.agent import render, transport
from tbhbdev.alpha.agent.decisions import argv
from tbhbdev.alpha.agent.decisions import attach as commands
from tbhbdev.alpha.agent.model import RunKind

if TYPE_CHECKING:
    from tbhbdev.alpha.agent.driver import Run
    from tbhbdev.alpha.agent.model import Settings


def run(settings: Settings, target: Run, *, read_only: bool) -> int:
    if target.request.kind is not RunKind.START:
        print(
            transport.terminal_text(
                f"{target.name}: attach applies only to a start run; use logs"
            ),
            file=sys.stderr,
        )
        return 1
    if target.request.unsandboxed:
        print(
            render.unsandboxed(target.name, target.request.authorized_by),
            file=sys.stderr,
            flush=True,
        )
    args = argv.ssh(
        target.host,
        commands.command(
            target.name, settings.sockets_dir, owner=target.owner, read_only=read_only
        ),
        connect_timeout=settings.ssh_connect_timeout,
        keepalive=settings.ssh_keepalive_seconds,
        keepalive_count_max=settings.ssh_keepalive_count_max,
    )
    code = subprocess.run(  # nosec B603 -- fixed ssh executable and quoted command
        (args[0], "-t", *args[1:]), check=False
    ).returncode
    if code == transport.COLLISION:
        message = transport.terminal_text(f"{target.name}: not owner")
        raise transport.CollisionError(message)
    return code

"""Bounded ssh calls and one private input stream; no lifecycle decisions."""

from __future__ import annotations

import asyncio
import io
import tarfile
from dataclasses import dataclass
from typing import TYPE_CHECKING

from tbhbdev.alpha.agent.decisions import argv, remote
from tbhbdev.alpha.agent.decisions import runfiles_names as files

if TYPE_CHECKING:
    from tbhbdev.alpha.agent.model import Host, Settings


@dataclass(frozen=True)
class Reading:
    phase: str
    rc: int | None
    launched: bool
    session: bool
    text: str


class TransportError(OSError):
    """A failed or timed-out ssh call, whose output contains no input bytes."""


def call(
    settings: Settings,
    host: Host,
    command: tuple[str, ...],
    *,
    data: bytes = b"",
    timeout: float = 30,
) -> str:
    args = argv.ssh(
        host,
        command,
        connect_timeout=settings.ssh_connect_timeout,
        keepalive=settings.ssh_keepalive_seconds,
        keepalive_count_max=settings.ssh_keepalive_count_max,
    )
    try:
        code, output = asyncio.run(
            asyncio.wait_for(execute(args, data), max(0.001, timeout))
        )
    except (OSError, TimeoutError) as exc:
        message = f"{host.name}: transport failed"
        raise TransportError(message) from exc
    if code:
        message = f"{host.name}: remote command failed ({code})"
        raise TransportError(message)
    return output.decode()


async def execute(args: tuple[str, ...], data: bytes) -> tuple[int, bytes]:
    """Execute argv directly and reap the owned ssh process on a deadline."""
    process = await asyncio.create_subprocess_exec(
        *args,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        output, _ = await process.communicate(data)
    except asyncio.CancelledError:
        process.kill()
        await process.wait()
        raise
    return process.returncode or 0, output


def shell(
    settings: Settings,
    host: Host,
    script: str,
    *,
    timeout: float = 30,
    data: bytes = b"",
) -> str:
    return call(settings, host, ("sh", "-c", script), timeout=timeout, data=data)


@dataclass(frozen=True)
class Inputs:
    request: str
    prompt: str
    schema: str | None


def stream(
    settings: Settings,
    host: Host,
    name: str,
    *,
    inputs: Inputs,
    timeout: float,
) -> None:
    entries = {files.REQUEST: inputs.request, files.PROMPT: inputs.prompt}
    if inputs.schema is not None:
        entries[files.SCHEMA] = inputs.schema
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        for suffix, text in entries.items():
            content = text.encode()
            info = tarfile.TarInfo(name + suffix)
            info.mode = 0o600
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))
    shell(settings, host, remote.transfer(), data=buffer.getvalue(), timeout=timeout)


def read(
    settings: Settings,
    host: Host,
    name: str,
    *,
    answer: bool = False,
    timeout: float = 30,
) -> Reading:
    text = shell(settings, host, remote.read(name, answer=answer), timeout=timeout)
    phase, rc, launched, session, tail = text.split("\n", 4)
    return Reading(
        phase, None if not rc else int(rc), launched == "1", session == "1", tail
    )

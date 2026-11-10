"""Bounded ssh calls and one private input stream; no lifecycle decisions."""

from __future__ import annotations

import asyncio
import io
import re
import tarfile
import unicodedata
from dataclasses import dataclass
from typing import TYPE_CHECKING

from tbhbdev.alpha.agent.decisions import argv, remote
from tbhbdev.alpha.agent.decisions import runfiles_names as files
from tbhbdev.alpha.agent.model import Invalid

if TYPE_CHECKING:
    from tbhbdev.alpha.agent.model import Host, Settings


COLLISION = 73
MAX_RC = 255


@dataclass(frozen=True)
class Reading:
    phase: str
    rc: int | None
    launched: bool
    session: bool
    text: str


class CollisionError(OSError):
    """The remote name belongs to another invocation."""


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
    if code == COLLISION:
        message = f"{host.name}: run name collision"
        raise CollisionError(message)
    if code:
        message = f"{host.name}: remote command failed ({code})"
        raise TransportError(message)
    return output.decode(errors="replace")


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
    return call(settings, host, ("/bin/sh", "-c", script), timeout=timeout, data=data)


@dataclass(frozen=True)
class Inputs:
    request: str
    prompt: str
    schema: str | None
    owner: str


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
    shell(
        settings,
        host,
        remote.transfer(name, settings.sockets_dir, owner=inputs.owner),
        data=buffer.getvalue(),
        timeout=timeout,
    )


def read(
    settings: Settings,
    host: Host,
    target: tuple[str, str],
    *,
    answer: bool = False,
    timeout: float = 30,
) -> Reading:
    name, owner = target
    text = shell(
        settings,
        host,
        remote.read(name, settings.sockets_dir, owner=owner, answer=answer),
        timeout=timeout,
    )
    phase, rc, launched, session, tail = text.split("\n", 4)
    phase = bytes.fromhex(phase).decode(errors="replace").removesuffix("\n")
    if re.fullmatch(r"[a-z-]{1,32}", phase) is None:
        message = f"{name}: phase=invalid"
        raise Invalid(message)
    value = None
    if rc != "-":
        rc = bytes.fromhex(rc).decode(errors="replace").removesuffix("\n")
        if re.fullmatch(r"[0-9]{1,3}", rc) is None or int(rc) > MAX_RC:
            message = f"{name}: rc=invalid"
            raise Invalid(message)
        value = int(rc)
    tail = "".join(
        c
        for c in tail
        if c in {"\n", "\t"} or not unicodedata.category(c).startswith("C")
    )
    return Reading(phase, value, launched == "1", session == "1", tail)

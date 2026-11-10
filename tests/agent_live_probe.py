"""Disposable upstream proxy and mirror objects for the live sandbox probes."""

from __future__ import annotations

import dataclasses
import hashlib
import select
import shlex
import socket
import threading
import zlib
from contextlib import contextmanager, suppress
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Generator
    from pathlib import Path


@dataclasses.dataclass
class Proxy:
    port: int
    targets: list[str] = dataclasses.field(default_factory=list)
    direct: list[bool] = dataclasses.field(default_factory=list)

    @property
    def url(self) -> str:
        return f"http://localhost:{self.port}"


def relay(
    first: socket.socket, second: socket.socket, stopped: threading.Event
) -> None:
    while not stopped.is_set():
        ready, _, _ = select.select((first, second), (), (), 0.05)
        for source in ready:
            data = source.recv(65536)
            if not data:
                return
            (second if source is first else first).sendall(data)


def request_header(connection: socket.socket) -> bytes:
    header = b""
    while b"\r\n\r\n" not in header and len(header) < 8192:
        part = connection.recv(4096)
        if not part:
            break
        header += part
    return header.split(b"\r\n", 1)[0]


def proxy_connection(
    connection: socket.socket, proxy: Proxy, stopped: threading.Event
) -> None:
    with connection, suppress(OSError, ValueError):
        connection.settimeout(3)
        parts = request_header(connection).decode("ascii").split()
        if len(parts) != 3:
            return
        if parts[0] != "CONNECT":
            proxy.direct.append(True)
            connection.sendall(b"HTTP/1.0 400 Bad Request\r\n\r\n")
            return
        host, port = parts[1].rsplit(":", 1)
        with socket.create_connection((host, int(port)), timeout=3) as upstream:
            proxy.targets.append(parts[1])
            connection.sendall(b"HTTP/1.0 200 Connection established\r\n\r\n")
            relay(connection, upstream, stopped)


def accept_proxy(
    server: socket.socket,
    proxy: Proxy,
    stopped: threading.Event,
    workers: list[threading.Thread],
) -> None:
    for connection in server_connections(server, stopped):
        worker = threading.Thread(
            target=proxy_connection, args=(connection, proxy, stopped)
        )
        workers.append(worker)
        worker.start()


def server_connections(
    server: socket.socket, stopped: threading.Event
) -> Generator[socket.socket]:
    while not stopped.is_set():
        try:
            connection, _ = server.accept()
        except TimeoutError:
            continue
        yield connection


@contextmanager
def control_proxy() -> Generator[Proxy]:
    stopped = threading.Event()
    workers: list[threading.Thread] = []
    with socket.socket(socket.AF_INET) as server:
        server.bind(("localhost", 0))
        server.listen()
        server.settimeout(0.05)
        proxy = Proxy(server.getsockname()[1])
        acceptor = threading.Thread(
            target=accept_proxy, args=(server, proxy, stopped, workers)
        )
        acceptor.start()
        try:
            yield proxy
        finally:
            stopped.set()
            acceptor.join(timeout=5)
            assert not acceptor.is_alive()
            for worker in workers:
                worker.join(timeout=5)
                assert not worker.is_alive()


def mirror_object(root: Path) -> Path:
    directory = root / "mirrors" / "example-org" / "example-repo.git"
    directory.mkdir(mode=0o700, parents=True)
    data = b"blob 7\0marker\n"
    name = hashlib.sha1(data, usedforsecurity=False).hexdigest()
    path = directory / "objects" / name[:2] / name[2:]
    path.parent.mkdir(parents=True)
    path.write_bytes(zlib.compress(data))
    return path


def mirror_probes(
    object_file: Path, workspace: Path, python: str, ln: str
) -> tuple[tuple[str, tuple[str, ...]], ...]:
    root = object_file.parents[2]
    alternates = workspace / ".git" / "objects" / "info" / "alternates"
    replacement = workspace / "replacement"
    linked = workspace / "linked"
    writes = (
        ("mirror-modify", f"Path({str(object_file)!r}).write_text('changed')"),
        ("mirror-create", f"Path({str(root / 'new')!r}).write_text('changed')"),
        (
            "mirror-replace",
            f"p=Path({str(replacement)!r});p.write_text('changed');p.rename({str(object_file)!r})",
        ),
        (
            "alternates",
            (
                f"p=Path({str(alternates)!r});"
                "p.parent.mkdir(parents=True,exist_ok=True);"
                f"p.write_text({str(root / 'objects')!r});"
                f"(Path(p.read_text())/{object_file.parent.name!r}/"
                f"{object_file.name!r}).write_text('changed')"
            ),
        ),
    )
    commands = tuple(
        (name, (python, "-c", f"from pathlib import Path;{code}"))
        for name, code in writes
    )
    link = (
        f"{shlex.join((ln, str(object_file), str(linked)))} && "
        f"printf changed > {shlex.quote(str(linked))}"
    )
    return (*commands, ("hard-link", ("/bin/sh", "-c", link)))

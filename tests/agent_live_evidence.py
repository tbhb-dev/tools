"""Independent mirror fixtures let the host judge every sandbox operation."""

from __future__ import annotations

import dataclasses
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from agent_live_probe import mirror_object, mirror_probes

from tbhbdev.alpha.agent import credentials, run_host, runfiles
from tbhbdev.alpha.agent.model import Harness

if TYPE_CHECKING:
    from collections.abc import Generator

    from tbhbdev.alpha.agent.model import RunPaths


@contextmanager
def codex_auth(
    run: run_host.Run, paths: RunPaths, account_home: Path, root: Path
) -> Generator[RunPaths]:
    assert run.request.harness is Harness.CODEX
    directory = Path(paths.codex_home)
    assert directory.resolve().is_relative_to(root.resolve())
    runfiles.private_dir(directory)
    before = None
    try:
        before = credentials.auth_copy(
            account_home / ".codex" / "auth.json", directory / "auth.json"
        )
        yield paths
    finally:
        prepared = run_host.Prepared(
            paths,
            credentials.Delivery({}, None, gh_fallback=False, claude_profile=None),
            before,
        )
        reason = run_host.finish_auth(
            dataclasses.replace(run, home=account_home), prepared
        )
        assert reason is None, reason
        assert not list(root.rglob("auth.json"))


MIRROR_LABELS = (
    "mirror-modify",
    "mirror-create",
    "mirror-replace",
    "alternates",
    "hard-link",
)
EXTRA_LABELS = (*MIRROR_LABELS, "file-control", "local-binding", "host-loopback")
ALL_LABELS = (
    "TMPDIR",
    "tmp",
    "HOME",
    "api.github.com",
    "example.com",
    *EXTRA_LABELS,
    "project-hooks",
)


@dataclass(frozen=True)
class MirrorProbe:
    label: str
    object_file: Path
    original: bytes
    links: int
    entries: frozenset[str]
    command: tuple[str, ...]

    def unchanged(self) -> bool:
        try:
            return (
                not self.object_file.is_symlink()
                and self.object_file.read_bytes() == self.original
                and self.object_file.stat().st_nlink == self.links
                and frozenset(p.name for p in self.object_file.parents[2].iterdir())
                == self.entries
            )
        except OSError:
            return False


def mirror_cases(
    root: Path, workspace: Path, python: str, ln: str
) -> tuple[MirrorProbe, ...]:
    cases = []
    for label in MIRROR_LABELS:
        object_file = mirror_object(root / label)
        target = workspace / label
        target.mkdir()
        command = dict(mirror_probes(object_file, target, python, ln))[label]
        cases.append(
            MirrorProbe(
                label,
                object_file,
                object_file.read_bytes(),
                object_file.stat().st_nlink,
                frozenset(p.name for p in object_file.parents[2].iterdir()),
                command,
            )
        )
    return tuple(cases)

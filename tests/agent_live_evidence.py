"""Independent mirror fixtures let the host judge every sandbox operation."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from agent_live_probe import mirror_object, mirror_probes

from tbhbdev.alpha.agent.model import Invalid


def prepared_codex_home(account_home: Path) -> str:
    configured = os.environ.get("TBHBDEV_SANDBOX_CODEX_HOME")
    if not configured:
        message = "set TBHBDEV_SANDBOX_CODEX_HOME to a prepared private Codex home"
        raise Invalid(message)
    directory = Path(configured).resolve()
    if not directory.is_dir() or directory == (account_home / ".codex").resolve():
        message = "TBHBDEV_SANDBOX_CODEX_HOME must name a separate prepared directory"
        raise Invalid(message)
    return str(directory)


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

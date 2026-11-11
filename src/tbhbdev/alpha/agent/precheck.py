"""Read a clone's hooks and config without invoking Git."""

from __future__ import annotations

import argparse
import base64
import re
import subprocess  # nosec B404
import sys
from dataclasses import dataclass
from pathlib import Path

RISKY_PATTERN = (
    r"hookspath|fsmonitor|sshcommand|^[[:space:]]*\["
    r"(credential|alias|filter|diff|include|includeif|url)"
)

# Keep the wire format shared by local collection and the single SSH call.
COLLECTOR = r"""
shopt -s nullglob
c=$1; case $c in /*) ;; *) c=$HOME/$c ;; esac
g=$c/.git
[ -d "$g/hooks" ] || { echo "@@ERR no clone at $g"; exit 0; }
for f in "$g"/hooks/*; do
  n=${f##*/}; case $n in *.sample) continue ;; esac
  echo "@@HOOK $n $(base64 < "$f" | tr -d '\n')"
done
echo "@@FILES"
files=("$g/config")
for f in "$g/config.worktree" "$g"/worktrees/*/config*; do files+=("$f"); done
for f in "${files[@]}"; do [ -e "$f" ] && echo "$f"; done
echo "@@RISKY"
for f in "${files[@]}"; do [ -e "$f" ] && grep -HniE 'PATTERN' "$f"; done
echo "@@END"
""".replace("PATTERN", RISKY_PATTERN)


@dataclass(frozen=True)
class CloneData:
    label: str
    hooks: dict[str, bytes]
    files: tuple[str, ...]
    risky: tuple[str, ...]
    error: str | None = None


@dataclass(frozen=True)
class Result:
    exit_code: int
    lines: tuple[str, ...]


def home_path(value: str) -> Path:
    return Path.home() / value.removeprefix("~/")


def parse_collection(output: str, label: str) -> CloneData:
    lines = output.split("\n")
    errors = tuple(
        line.removeprefix("@@ERR ") for line in lines if line.startswith("@@ERR")
    )
    if errors:
        return CloneData(label, {}, (), (), "\n".join(errors))
    if "@@END" not in lines:
        message = f"truncated output from {label}"
        raise ValueError(message)
    hooks: dict[str, bytes] = {}
    for line in lines:
        match = re.fullmatch(r"@@HOOK ([^ ]+) (.*)", line)
        if match:
            hooks[match[1]] = base64.b64decode(match[2])
    return CloneData(
        label,
        hooks,
        tuple(lines[lines.index("@@FILES") + 1 : lines.index("@@RISKY")]),
        tuple(lines[lines.index("@@RISKY") + 1 : lines.index("@@END")]),
    )


def collect(clone: str, host: str | None = None) -> CloneData:
    command = ["bash", "-s", "--", clone.removeprefix("~/")]
    label = str(home_path(clone)) if host is None else host
    if host is not None:
        command = ["ssh", host, *command]
    try:
        completed = subprocess.run(  # nosec B603
            command,
            input=COLLECTOR.encode(),
            stdout=subprocess.PIPE,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        message = f"ssh to {label} failed"
        raise ValueError(message) from exc
    return parse_collection(completed.stdout.decode(errors="surrogateescape"), label)


def normalize(content: bytes) -> bytes:
    return re.sub(
        rb'^PREK="[^"\n]*/prek/[^/"\n]+/prek-[^/"\n]+/prek"$',
        b'PREK="<prek>"',
        content,
        flags=re.MULTILINE,
    )


def precheck(clone_data: CloneData, reference: Path) -> Result:
    """Compare collected bytes with a local reference and return printable results."""
    reference = home_path(str(reference))
    hooks = reference / ".git" / "hooks"
    if not hooks.is_dir():
        message = f"no reference clone at {reference}/.git"
        raise ValueError(message)
    if clone_data.error is not None:
        return Result(1, (clone_data.error, "PRECHECK FAIL"))
    expected = sorted(
        p.name
        for p in hooks.glob("*")
        if not p.name.startswith(".") and not p.name.endswith(".sample")
    )
    actual = sorted(clone_data.hooks)
    mismatches: list[str] = []
    if expected != actual:
        mismatches.append(
            f"MISMATCH hook set: reference [{' '.join(expected)}] vs "
            f"{clone_data.label} [{' '.join(actual)}]"
        )
    mismatches.extend(
        f"MISMATCH hook {name} differs from the reference beyond the prek version"
        for name in expected
        if name in clone_data.hooks
        and normalize((hooks / name).read_bytes()) != normalize(clone_data.hooks[name])
    )
    mismatches.extend(f"MISMATCH risky config: {line}" for line in clone_data.risky)
    checked = "checked: " + "".join(f"{path} " for path in clone_data.files)
    return Result(
        1 if mismatches else 0,
        (*mismatches, checked, "PRECHECK FAIL" if mismatches else "PRECHECK OK"),
    )


def main(argv: list[str], prog: str) -> int:
    parser = argparse.ArgumentParser(prog=prog, description=__doc__)
    parser.add_argument("clone", metavar="CLONE")
    parser.add_argument("--reference", required=True, metavar="REF")
    parser.add_argument("--host")
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return 2 if exc.code else 0
    reference = home_path(args.reference)
    if not (reference / ".git" / "hooks").is_dir():
        print(f"no reference clone at {reference}/.git", file=sys.stderr)
        return 2
    try:
        result = precheck(collect(args.clone, args.host), reference)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print("\n".join(result.lines))
    return result.exit_code

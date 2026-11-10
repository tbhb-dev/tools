"""Remote argv and shell reads. These return text; transport executes it."""

from __future__ import annotations

import shlex
from pathlib import PurePosixPath

from tbhbdev.alpha.agent.decisions import argv, repo
from tbhbdev.alpha.agent.decisions import runfiles_names as files

ROOT = f'd="${{XDG_STATE_HOME:-$HOME/.local/state}}/{files.DIRECTORY}"\n'
GIT = "git -c core.hooksPath=/dev/null"


def session(name: str, *command: str) -> str:
    return shlex.join(("tmux", "-L", argv.SOCKET, *command, "-t", f"ag-{name}"))


def preflight(repository: str, ref: str, expected_version: str) -> str:
    # The clone path is relative to the remote account's home.
    clone = repo.clone_path(PurePosixPath("."), repository)
    return (
        'installed=$(tbhbdev --version) || exit "$?"\n'
        "printf '%s\\n' \"$installed\"\n"
        f'test "$installed" = {shlex.quote(expected_version)} || exit 0\n'
        f'cd "$HOME"/{shlex.quote(str(clone))} || exit "$?"\n'
        f"{GIT} ls-remote --exit-code -- origin {shlex.quote(ref)} "
        f"{shlex.quote(ref + '^{}')}"
    )


def transfer() -> str:
    return ROOT + 'umask 077\nmkdir -p "$d" && chmod 700 "$d" && tar -xf - -C "$d"'


def start(name: str) -> tuple[str, ...]:
    return (
        "tmux",
        "-L",
        argv.SOCKET,
        "new-session",
        "-d",
        "-s",
        f"ag-{name}",
        "tbhbdev",
        "alpha",
        "agent",
        "run-host",
        name,
    )


def read(name: str, *, answer: bool = False) -> str:
    phase, rc, launched = (
        name + suffix for suffix in (files.PHASE, files.RC, files.LAUNCHED)
    )
    header = (
        ROOT
        + f"""cd "$d" || exit "$?"
if test -f {phase}; then cat {phase}; else printf 'unknown'; fi
printf '\\n'
if test -f {rc}; then cat {rc}; fi
printf '\\n'
test ! -f {launched}; printf '%s\\n' "$?"
{session(name, "has-session")} 2>/dev/null; test "$?" -ne 0; printf '%s\\n' "$?"
"""
    )
    # Strip file newlines on the first two fields, keeping a four-line header.
    header = header.replace(f"cat {phase}", f"printf '%s' \"$(cat {phase})\"")
    header = header.replace(f"cat {rc}", f"printf '%s' \"$(cat {rc})\"")
    if answer:
        return (
            header
            + f"""if test -f {name}{files.FINAL}; then tail -n 40 {name}{files.FINAL}
elif test -f {name}{files.OUT}; then tail -n 40 {name}{files.OUT}; fi
"""
        )
    return (
        header
        + f"if test -f {name}{files.LOG}; then head -n 20 {name}{files.LOG}; fi\n"
    )


def fallback(name: str) -> str:
    rc = name + files.RC
    pending = name + files.RC_PENDING
    return (
        ROOT
        + f"""umask 077
cd "$d" || exit "$?"
if {session(name, "has-session")} 2>/dev/null; then exit 1; fi
if test ! -f {rc}; then printf '137\\n' > {pending} && mv -n {pending} {rc}; fi
"""
    )


def keep(name: str) -> str:
    return ROOT + f'umask 077\n: > "$d/{name}{files.KEEP}"'


def branch_facts(repository: str, name: str) -> str:
    clone = repo.clone_path(PurePosixPath("."), repository)
    branch = f"agent/{name}"
    upstream = f"{GIT} rev-parse --symbolic-full-name {branch}@{{upstream}}"
    return (
        f'cd "$HOME"/{shlex.quote(str(clone))} || exit "$?"\n'
        f"if {GIT} show-ref --verify --quiet refs/heads/{branch}; then\n"
        f"  if upstream=$({upstream} 2>/dev/null); then\n"
        f'    {GIT} rev-list --count {branch} "^$upstream"\n'
        "  else printf '1\\n'; fi\n"
        "else printf '0\\n'; fi\n"
    )


def clean(
    repository: str, name: str, stamp: str, *, force: bool, delete_branch: bool
) -> str:
    clone = repo.clone_path(PurePosixPath("."), repository)
    tree = f".worktrees/agent-{name}"
    branch = f"agent/{name}"
    removal = f"{GIT} worktree remove " + ("--force " if force else "") + tree
    # Recheck liveness immediately before touching the worktree or files.
    command = (
        ROOT
        + f"""if {session(name, "has-session")} 2>/dev/null; then exit 1; fi
cd "$HOME"/{shlex.quote(str(clone))} || exit "$?"
if test -d {tree}; then {removal} || exit "$?"; fi
"""
    )
    if delete_branch:
        command += f"""if {GIT} show-ref --verify --quiet refs/heads/{branch}; then
{GIT} branch -D {branch} || exit "$?"
fi
"""
    command += (
        f'umask 077\ntrash="$d/trash/{stamp}-{name}"\nmkdir -p "$trash" || exit "$?"\n'
    )
    for suffix in files.SUFFIXES:
        path = f'"$d/{name}{suffix}"'
        command += f'if test -e {path}; then mv {path} "$trash/" || exit "$?"; fi\n'
    return command

"""Remote shell scripts over fixed executables and untrusted run data."""

from __future__ import annotations

import shlex
from pathlib import PurePosixPath
from typing import TYPE_CHECKING

from tbhbdev.alpha.agent.decisions import repo
from tbhbdev.alpha.agent.decisions import runfiles_names as files

if TYPE_CHECKING:
    from tbhbdev.alpha.agent.model import Host

PREFIX = """PATH=/usr/bin:/bin:/usr/sbin:/sbin
export PATH
if test "$(uname -s)" = Darwin; then
PATH="$PATH:/opt/homebrew/bin:/usr/local/bin"
export PATH
fi
"""
ROOT = f"""state="${{XDG_STATE_HOME:-$HOME/.local/state}}"
for p in "$state" "$state/tbhbdev" "$state/{files.DIRECTORY}"; do
    test ! -L "$p" || exit 65
done
d="$state/{files.DIRECTORY}"
"""
# Copy this boundary until the host and driver branches can share it.
GIT = (
    'env -i PATH="$PATH" HOME="$HOME" LANG=C GIT_TERMINAL_PROMPT=0 '
    "GIT_CONFIG_NOSYSTEM=1 GIT_ASKPASS=/bin/false SSH_ASKPASS=/bin/false "
)
GIT_FLAGS = (
    "git -c core.hooksPath=/dev/null -c core.fsmonitor=false "
    "-c core.sshCommand=ssh -c credential.helper= -c core.askPass= "
    "-c protocol.ext.allow=never -c core.pager=cat"
)
HELPERS = r"""safe_file() {
    test ! -L "$1" || exit 65
    if test -e "$1"; then test -f "$1" || exit 65; fi
}
field() {
    safe_file "$1"
    if test -f "$1"; then
        head -c 64 "$1" | od -An -v -tx1 | tr -d ' \n'
    else printf '%s' "$2"; fi
    printf '\n'
}
tmux_state() {
    error=$(tmux -S "$s/agents" "$1" -t "=$2" 2>&1)
    code=$?
    present=1
    if test "$code" -eq 0; then return; fi
    test "$code" -eq 1 || exit 75
    case "$error" in
        "can't find session:"*|"no server running on "*) present=0 ;;
        "error connecting to "*" (No such file or directory)") present=0 ;;
        *) exit 75 ;;
    esac
}
"""


def socket_root(sockets_dir: str) -> str:
    path = shlex.quote(sockets_dir)
    if not sockets_dir.startswith("/"):
        path = '"$HOME"/' + shlex.quote(sockets_dir)
    return (
        PREFIX
        + ROOT
        + f"s={path}\n"
        + """umask 077
if test ! -e "$s"; then mkdir -p -m 700 "$s" || exit "$?"; fi
test ! -L "$s" && test ! -L "$s/agents" || exit 65
test "$(find "$s" -prune -type d -user "$(id -un)" -perm 0700)" = "$s" || exit 65
"""
        + HELPERS
    )


def session(name: str, command: str, sockets_dir: str) -> str:
    return socket_root(sockets_dir) + f"tmux_state {command} ag-{name}\n"


def program(host: Host) -> str:
    return (
        shlex.quote(host.tbhbdev)
        if host.tbhbdev is not None
        else '"$HOME/.local/bin/tbhbdev"'
    )


def installed(host: Host) -> str:
    return PREFIX + f"{program(host)} --version"


def preflight(repository: str, ref: str, expected_version: str, host: Host) -> str:
    git_host, owner, project = repo.parts(repository)
    url = f"git@{git_host}:{owner}/{project}.git"
    return (
        PREFIX + f'installed=$({program(host)} --version) || exit "$?"\n'
        "printf '%s\\n' \"$installed\"\n"
        f'test "$installed" = {shlex.quote(expected_version)} || exit 0\n'
        'cd "$HOME" || exit "$?"\n'
        + GIT
        + 'SSH_AUTH_SOCK="${SSH_AUTH_SOCK:-}" '
        + GIT_FLAGS
        + f" ls-remote --exit-code -- {shlex.quote(url)} {shlex.quote(ref)} "
        f"{shlex.quote(ref + '^{}')}"
    )


def transfer(name: str, sockets_dir: str) -> str:
    return (
        socket_root(sockets_dir)
        + f"""tmux_state has-session ag-{name}
test "$present" -eq 0 || exit 73
mkdir -p "$d" && chmod 700 "$d" || exit "$?"
# The first input file is also the atomic name claim.
set -C
for p in "$d/{name}."*; do
    if test -e "$p" || test -L "$p"; then exit 73; fi
done
( : > "$d/{name}{files.REQUEST}" ) || exit 73
tar -xf - -C "$d"
"""
    )


def start(name: str, sockets_dir: str, host: Host) -> tuple[str, ...]:
    command = shlex.join(("alpha", "agent", "run-host", name))
    return (
        "/bin/sh",
        "-c",
        socket_root(sockets_dir)
        + (
            f'tmux -S "$s/agents" new-session -d -s ag-{name} '
            + shlex.quote(f"{program(host)} {command}")
        ),
    )


def read(name: str, sockets_dir: str, *, answer: bool = False) -> str:
    header = (
        socket_root(sockets_dir)
        + f"""tmux_state has-session ag-{name}
field "$d/{name}{files.PHASE}" 756e6b6e6f776e
field "$d/{name}{files.RC}" -
safe_file "$d/{name}{files.LAUNCHED}"
if test -f "$d/{name}{files.LAUNCHED}"; then printf '1\\n'; else printf '0\\n'; fi
printf '%s\\n' "$present"
"""
    )
    final, output, log = (
        f'"$d/{name}{suffix}"' for suffix in (files.FINAL, files.OUT, files.LOG)
    )
    if answer:
        return (
            header
            + f"""safe_file {final}
safe_file {output}
if test -f {final}; then tail -c 65536 {final}
elif test -f {output}; then tail -c 65536 {output}; fi
"""
        )
    return header + f"safe_file {log}\nif test -f {log}; then head -c 65536 {log}; fi\n"


def fallback(name: str, sockets_dir: str) -> str:
    rc, pending = (f'"$d/{name}{suffix}"' for suffix in (files.RC, files.RC_PENDING))
    return (
        socket_root(sockets_dir)
        + f"""tmux_state has-session ag-{name}
test "$present" -eq 0 || exit 1
mkdir -p "$d" && chmod 700 "$d" || exit "$?"
safe_file {rc}
safe_file {pending}
set -C
if test ! -f {rc}; then printf '137\\n' > {pending} && mv -n {pending} {rc}; fi
"""
    )


def keep(name: str) -> str:
    return (
        PREFIX
        + ROOT
        + f"""umask 077
mkdir -p "$d" && chmod 700 "$d" || exit "$?"
set -C
test ! -L "$d/{name}{files.KEEP}" || exit 65
: > "$d/{name}{files.KEEP}"
"""
    )


def branch_facts(repository: str, name: str) -> str:
    clone = repo.clone_path(PurePosixPath("."), repository)
    branch = f"agent/{name}"
    git = GIT + GIT_FLAGS
    upstream = f"{git} rev-parse --symbolic-full-name {branch}@{{upstream}}"
    return (
        PREFIX + f'if test ! -d "$HOME"/{shlex.quote(str(clone))}; '
        "then printf '0\\n'; exit 0; fi\n"
        f'cd "$HOME"/{shlex.quote(str(clone))} || exit "$?"\n'
        f"if {git} show-ref --verify --quiet refs/heads/{branch}; then\n"
        f"  if upstream=$({upstream} 2>/dev/null); then\n"
        f'    {git} rev-list --count {branch} "^$upstream"\n'
        "  else printf '1\\n'; fi\n"
        "else printf '0\\n'; fi\n"
    )


def clean(
    repository: str, name: str, stamp: str, sockets_dir: str, *, delete_branch: bool
) -> str:
    clone = repo.clone_path(PurePosixPath("."), repository)
    tree = f".worktrees/agent-{name}"
    branch = f"agent/{name}"
    git = GIT + GIT_FLAGS
    command = (
        socket_root(sockets_dir)
        + f"""tmux_state has-session ag-{name}
test "$present" -eq 0 || exit 1
trash="$d/trash/{stamp}-{name}"
test ! -L "$d/trash" || exit 65
mkdir -p "$d/trash" && mkdir "$trash" || exit "$?"
if test -d "$HOME"/{shlex.quote(str(clone))}; then
cd "$HOME"/{shlex.quote(str(clone))} || exit "$?"
test ! -L {tree} || exit 65
if test -d {tree}; then mv {tree} "$trash/worktree" || exit "$?"; fi
{git} worktree prune || exit "$?"
"""
    )
    if delete_branch:
        command += f"""if {git} show-ref --verify --quiet refs/heads/{branch}; then
{git} branch -D {branch} || exit "$?"
fi
"""
    command += "fi\n"
    for suffix in files.SUFFIXES:
        path = f'"$d/{name}{suffix}"'
        command += (
            f"safe_file {path}\n"
            f'if test -f {path}; then mv {path} "$trash/" || exit "$?"; fi\n'
        )
    return command

"""Remote shell scripts over fixed executables and untrusted run data."""

from __future__ import annotations

import shlex
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import TYPE_CHECKING

from tbhbdev.alpha.agent.decisions import repo
from tbhbdev.alpha.agent.decisions import runfiles_names as files

if TYPE_CHECKING:
    from tbhbdev.alpha.agent.model import Host

PREFIX = """PATH=/usr/bin:/bin:/usr/sbin:/sbin
export PATH
LC_ALL=C
LANG=C
export LC_ALL LANG
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
    'env -i PATH="$PATH" HOME="$HOME" LANG=C LC_ALL=C '
    "GIT_TERMINAL_PROMPT=0 GIT_NO_LAZY_FETCH=1 "
    "GIT_CONFIG_NOSYSTEM=1 GIT_ASKPASS=/bin/false SSH_ASKPASS=/bin/false "
)
GIT_FLAGS = (
    "git -c core.hooksPath=/dev/null -c core.fsmonitor=false "
    "-c core.sshCommand=ssh -c credential.helper= -c core.askPass= "
    "-c protocol.ext.allow=never -c core.pager=cat "
    "-c gc.auto=0 -c maintenance.auto=false"
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
    error=$(tmux -S "$s/agents" -f /dev/null "$1" -t "=$2" 2>&1)
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


def owned(name: str, owner: str) -> str:
    path = f'"$d/{name}{files.OWNER}"'
    return (
        PREFIX
        + ROOT
        + f"test ! -L {path} && test -f {path} && "
        + f'test "$(cat {path})" = {shlex.quote(owner)} || exit 73\n'
    )


def session(name: str, command: str, sockets_dir: str, *, owner: str) -> str:
    return (
        owned(name, owner)
        + socket_root(sockets_dir)
        + f"tmux_state {command} ag-{name}\n"
    )


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


def transfer(name: str, sockets_dir: str, *, owner: str) -> str:
    return (
        socket_root(sockets_dir)
        + f"""tmux_state has-session ag-{name}
test "$present" -eq 0 || exit 73
mkdir -p "$d" && chmod 700 "$d" || exit "$?"
# The owner is the atomic name claim, before extracting any input.
set -C
for p in "$d/{name}."*; do
    if test -e "$p" || test -L "$p"; then exit 73; fi
done
( printf '%s\\n' {shlex.quote(owner)} > "$d/{name}{files.OWNER}" ) || exit 73
tar -xf - -C "$d"
"""
    )


def start(name: str, sockets_dir: str, host: Host, *, owner: str) -> tuple[str, ...]:
    command = shlex.join(("alpha", "agent", "run-host", name, "--host", host.name))
    return (
        "/bin/sh",
        "-c",
        owned(name, owner)
        + socket_root(sockets_dir)
        + f'tmux -S "$s/agents" -f /dev/null new-session -d -s ag-{name} '
        + f"{program(host)} {command}",
    )


def read(name: str, sockets_dir: str, *, owner: str, answer: bool = False) -> str:
    header = (
        owned(name, owner)
        + socket_root(sockets_dir)
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


def fallback(name: str, sockets_dir: str, *, owner: str) -> str:
    rc, pending, marker = (
        f'"$d/{name}{suffix}"'
        for suffix in (files.RC, files.RC_PENDING, files.FALLBACK)
    )
    return (
        owned(name, owner)
        + socket_root(sockets_dir)
        + f"""tmux_state has-session ag-{name}
test "$present" -eq 0 || exit 1
safe_file {rc}
safe_file {pending}
safe_file {marker}
set -C
if test ! -f {rc}; then
    if test ! -f {marker}; then ( : > {marker} ) || exit "$?"; fi
    if test -f {pending}; then
        test ! -L "$d/trash" || exit 65
        mkdir -p "$d/trash" || exit "$?"
        stale=$(mktemp -d "$d/trash/{name}-pending.XXXXXXXX") || exit "$?"
        mv {pending} "$stale/" || exit "$?"
    fi
    printf '137\\n' > {pending} && mv -n {pending} {rc}
fi
"""
    )


def keep(name: str, *, owner: str) -> str:
    path = f'"$d/{name}{files.KEEP}"'
    return (
        owned(name, owner)
        + HELPERS
        + f"""umask 077
safe_file {path}
set -C
if test ! -f {path}; then ( : > {path} ); fi
"""
    )


def branch_facts(repository: str, name: str, *, owner: str) -> str:
    clone = repo.clone_path(PurePosixPath("."), repository)
    branch = f"agent/{name}"
    git = GIT + GIT_FLAGS + " -c protocol.allow=never"
    upstream = f"{git} rev-parse --symbolic-full-name {branch}@{{upstream}}"
    return (
        owned(name, owner) + f'if test ! -d "$HOME"/{shlex.quote(str(clone))}; '
        "then printf '0\\n'; exit 0; fi\n"
        f'cd "$HOME"/{shlex.quote(str(clone))} || exit "$?"\n'
        f"if {git} show-ref --verify --quiet refs/heads/{branch}; then\n"
        f"  if upstream=$({upstream} 2>/dev/null); then\n"
        f'    {git} rev-list --count {branch} "^$upstream"\n'
        "  else printf '1\\n'; fi\n"
        "else printf '0\\n'; fi\n"
    )


@dataclass(frozen=True)
class Cleanup:
    owner: str
    delete_branch: bool
    force: bool


def clean(
    repository: str,
    name: str,
    stamp: str,
    sockets_dir: str,
    *,
    decision: Cleanup,
) -> str:
    clone = repo.clone_path(PurePosixPath("."), repository)
    tree = f".worktrees/agent-{name}"
    branch = f"agent/{name}"
    git = GIT + GIT_FLAGS + " -c protocol.allow=never"
    locked = f"""move_directory() {{
    if test -L "$1"; then
        printf '%s\\n' "clean refused: $1 is a symlink" >&2; exit 65
    fi
    if test -e "$1"; then
        if test ! -d "$1"; then
            printf '%s\\n' "clean refused: $1 is not a directory" >&2; exit 65
        fi
        test ! -e "$2" && test ! -L "$2" || exit 65
        mv "$1" "$2" || exit "$?"
    fi
}}
move_directory {tree} "$trash/worktree"
test ! -L "$common/worktrees" || exit 65
move_directory "$common/worktrees/agent-{name}" "$trash/admin"
"""
    if decision.delete_branch:
        locked += f"""if {git} show-ref --verify --quiet refs/heads/{branch}; then
{git} branch -D {branch} || exit "$?"
fi
"""
    lock = (
        'open(my $lock, ">>", shift) or die "open lock: $!"; '
        'flock($lock, LOCK_EX) or die "lock: $!"; '
        'system("/bin/sh", "-c", shift); '
        "exit($? == -1 ? 1 : ($? & 127) ? 128 + ($? & 127) : $? >> 8);"
    )
    command = (
        owned(name, decision.owner)
        + socket_root(sockets_dir)
        + f"""tmux_state has-session ag-{name}
test "$present" -eq 0 || exit 1
"""
    )
    if not decision.force:
        command += f"""fallback="$d/{name}{files.FALLBACK}"
if test -e "$fallback" || test -L "$fallback"; then
    printf '%s\\n' 'clean refused: fallback rc requires --force' >&2
    exit 65
fi
"""
    command += f"""trash="$d/trash/{stamp}-{name}"
test ! -L "$d/trash" && test ! -L "$trash" || exit 65
mkdir -p "$trash" || exit "$?"
if test -d "$HOME"/{shlex.quote(str(clone))}; then
cd "$HOME"/{shlex.quote(str(clone))} || exit "$?"
common=$({git} rev-parse --path-format=absolute --git-common-dir) || exit "$?"
safe_file "$common/agent-worktree.lock"
export common trash
perl -MFcntl=:flock -e {shlex.quote(lock)} \
    "$common/agent-worktree.lock" {shlex.quote(locked)} || exit "$?"
fi
"""
    for suffix in files.SUFFIXES:
        path = f'"$d/{name}{suffix}"'
        command += (
            f"safe_file {path}\n"
            f"if test -f {path}; then\n"
            f'    target="$trash/{name}{suffix}"\n'
            '    test ! -e "$target" && test ! -L "$target" || exit 65\n'
            f'    mv {path} "$target" || exit "$?"\n'
            "fi\n"
        )
    return command

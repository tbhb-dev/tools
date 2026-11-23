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
PATH="$HOME/.local/bin:$PATH"
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
case "$state" in /*) ;; *) state="$HOME/.local/state" ;; esac
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
FILE_HELPERS = r"""safe_file() {
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
"""
HELPERS = (
    FILE_HELPERS
    + r"""tmux_state() {
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
)


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


def owned(name: str) -> str:
    """Check the owner on stdin's first line against the run's owner file.

    The owner never appears in the script, because ssh arguments reach audit
    logs. ``transport`` writes the owner as the first line of stdin.
    """
    path = f'"$d/{name}{files.OWNER}"'
    return (
        PREFIX
        + ROOT
        + 'IFS= read -r given || exit 73\ntest -n "$given" || exit 73\n'
        + f"test ! -L {path} && test -f {path} && "
        + f'test "$(cat {path})" = "$given" || exit 73\n'
    )


def session(name: str, command: str, sockets_dir: str) -> str:
    return owned(name) + socket_root(sockets_dir) + f"tmux_state {command} ag-{name}\n"


def program(host: Host) -> str:
    return (
        shlex.quote(host.tbhbdev)
        if host.tbhbdev is not None
        else '"$HOME/.local/bin/tbhbdev"'
    )


def installed(host: Host) -> str:
    return PREFIX + f"{program(host)} --version"


def host_marker(account: str) -> str:
    """Read bounded evidence of host taint, never a remote clearance."""
    # A marker that exists but can't be read prints a sentinel and exits 0, so
    # it counts as a real marker. A nonzero exit then means the ssh call failed.
    read = (
        'sub bad { print "unreadable\\n"; exit 0 } '
        'open my $f, "<", $ARGV[0] or do { exit 0 if $!{ENOENT}; bad(); }; '
        "defined(read($f, my $b, 65536)) or bad(); "
        "print $b; close($f) or bad();"
    )
    marker = PurePosixPath("/var", "tmp", "tbhbdev-agent-taint", f"{account}.jsonl")
    return PREFIX + "perl -e " + shlex.quote(read) + " " + shlex.quote(str(marker))


def limits() -> str:
    """Print one line of host numbers: load, CPUs, then memory and swap in KiB.

    The fields are the 1-minute load, the logical CPU count, total memory,
    available memory, swap total and swap used. macOS supplies zero for both
    swap fields so the launch check uses load and available memory alone.
    """
    linux = (
        "read l _ < /proc/loadavg || exit 1\n"
        "c=$(nproc) || exit 1\n"
        'awk -v l="$l" -v c="$c" \'/^MemTotal:/{t=$2} /^MemAvailable:/{a=$2} '
        "/^SwapTotal:/{s=$2} /^SwapFree:/{f=$2} "
        "END{if (t!~/^[0-9]+$/ || a!~/^[0-9]+$/ "
        "|| s!~/^[0-9]+$/ || f!~/^[0-9]+$/) exit 1; "
        "print l, c, t, a, s, s-f}' "
        "/proc/meminfo\n"
    )
    macos = r"""l=$(sysctl -n vm.loadavg) || exit 1
c=$(sysctl -n hw.logicalcpu) || exit 1
m=$(sysctl -n hw.memsize) || exit 1
v=$(vm_stat) || exit 1
printf '%s\n' "$v" | awk -v l="$l" -v c="$c" -v m="$m" '
/page size of/ { size = $8 }
/^Pages (free|inactive|speculative):/ {
    if ($NF !~ /^[0-9]+[.]?$/ || seen[$2]++) invalid = 1
    pages += $NF
    count++
}
END {
    split(l, load, " ")
    if (invalid || count != 3 || size !~ /^[0-9]+$/ || size == 0 ||
        m !~ /^[0-9]+$/ || load[2] == "") exit 1
    printf "%s %s %.0f %.0f 0 0\n", load[2], c, int(m/1024), int(pages*size/1024)
}'
"""
    return (
        "# tbhbdev-limits\n"
        + PREFIX
        + f'if test "$(uname -s)" = Darwin; then\n{macos}else\n{linux}fi\n'
    )


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
# The owner, stdin's first line, is the atomic name claim before the archive.
IFS= read -r given || exit 73
test -n "$given" || exit 73
set -C
for p in "$d/{name}."*; do
    if test -e "$p" || test -L "$p"; then exit 73; fi
done
( printf '%s\\n' "$given" > "$d/{name}{files.OWNER}" ) || exit 73
tar -xf - -C "$d"
"""
    )


def start(name: str, sockets_dir: str, host: Host) -> tuple[str, ...]:
    command = shlex.join(
        ("alpha", "agent", "run-host", name, "--host", host.name, "--owned")
    )
    return (
        "/bin/sh",
        "-c",
        owned(name)
        + socket_root(sockets_dir)
        + f'tmux -S "$s/agents" -f /dev/null new-session -d -s ag-{name} '
        + f"{program(host)} {command}",
    )


def read(name: str, sockets_dir: str, *, answer: bool = False) -> str:
    header = (
        owned(name)
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


def fallback(name: str, sockets_dir: str) -> str:
    rc, pending, marker = (
        f'"$d/{name}{suffix}"'
        for suffix in (files.RC, files.RC_PENDING, files.FALLBACK)
    )
    return (
        owned(name)
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


def keep(name: str) -> str:
    path = f'"$d/{name}{files.KEEP}"'
    return (
        owned(name)
        + HELPERS
        + f"""umask 077
safe_file {path}
set -C
if test ! -f {path}; then ( : > {path} ); fi
"""
    )


# The mirror lock serializes host fetches and reads of the fetched refs.
# Files and directories git reads inside a run's .git for a format-patch. The
# fallback refuses when any of them is a symlink.
GIT_INTERNALS = (
    "config",
    "HEAD",
    "objects",
    "refs",
    "packed-refs",
    "info",
    "shallow",
    "commondir",
)

MIRROR_LOCK = (
    'open(my $lock, ">>", shift) or die "open lock: $!"; '
    'flock($lock, LOCK_EX) or die "lock: $!"; '
    'system("/bin/sh", "-c", shift); '
    "exit($? == -1 ? 1 : ($? & 127) ? 128 + ($? & 127) : $? >> 8);"
)
MIRROR_GIT = (
    GIT
    + 'SSH_AUTH_SOCK="${SSH_AUTH_SOCK:-}" '
    + GIT_FLAGS
    + " -c fetch.recurseSubmodules=false "
    '-c submodule.recurse=false --git-dir="$mirror"'
)
# Read at most one byte past the display cap, then drain without buffering.
# Closing the pipe preserves the git command's failure status.
INSPECTION_OUTPUT = (
    'open(my $pipe, "-|", @ARGV) or die "open output: $!"; '
    'my $text = ""; '
    "while(length($text) < 65537 && "
    "read($pipe, $text, 65537 - length($text), length($text))) {} "
    "print substr($text, 0, 65536); "
    'print "\\n[output truncated at 65536 bytes]\\n" if length($text) > 65536; '
    "while(read($pipe, my $discard, 8192)) {} "
    "close($pipe); "
    "exit($? == -1 ? 1 : ($? & 127) ? 128 + ($? & 127) : $? >> 8);"
)


def mirror_command(repository: str, name: str, command: str) -> str:
    git_host, organization, project = repo.parts(repository)
    git_host = git_host.lower()
    return (
        owned(name)
        + HELPERS
        + f'mirror="$state/tbhbdev/agent-mirrors/{git_host}/{organization}/'
        f"""{project}.git"
for p in "$state/tbhbdev/agent-mirrors" \
    "$state/tbhbdev/agent-mirrors/{git_host}" \
    "$state/tbhbdev/agent-mirrors/{git_host}/{organization}" "$mirror"; do
    test ! -L "$p" && test -d "$p" || exit 65
done
# ls-remote can read parent config even with an invalid explicit directory.
test -d "$mirror/objects" && test -d "$mirror/refs" && \
    test -f "$mirror/HEAD" || exit 65
safe_file "$mirror/agent-mirror.lock"
export mirror
cd "$HOME" || exit "$?"
perl -MFcntl=:flock -e {shlex.quote(MIRROR_LOCK)} \
    "$mirror/agent-mirror.lock" {shlex.quote(command)}
"""
    )


def origin_branch(repository: str, name: str) -> str:
    return mirror_command(
        repository,
        name,
        f"{MIRROR_GIT} ls-remote --end-of-options origin refs/heads/agent/{name}",
    )


def inspect(repository: str, name: str, *, limit: int | None) -> str:
    branch = f"refs/heads/agent/{name}"
    base = "refs/tbhbdev/default"
    command = "diff" if limit is None else f"log -n {limit}"
    comparison = f"{base}...{branch}" if limit is None else f"{base}..{branch}"
    read = (
        f"{MIRROR_GIT} -c diff.external= --no-optional-locks {command} "
        f"--no-ext-diff --no-textconv --no-color {comparison} --"
    )
    fallback = ""
    if limit is None:
        tree = (
            repo.clone_path(PurePosixPath("."), repository)
            / ".worktrees"
            / f"agent-{name}"
        )
        paths = " ".join(
            '"$HOME"/' + shlex.quote(str(path))
            for path in (*reversed(tree.parents[:-1]), tree, tree / ".git")
        )
        internals = " ".join(
            '"$HOME"/' + shlex.quote(str(tree / ".git" / entry))
            for entry in GIT_INTERNALS
        )
        run = '"$HOME"/' + shlex.quote(str(tree / ".git"))
        # Git never reads the run's own config: format-patch runs in a fresh
        # bare repository and borrows only the run's object store, and the
        # two commits come from reading HEAD and the ref files as plain text.
        patch = (
            f"run={run}\n"
            "resolve() {\n"
            '    case "$1" in *[!A-Za-z0-9._/-]*|*..*|*//*) return 65 ;; esac\n'
            '    if test -f "$run/$1" && test ! -L "$run/$1"; then\n'
            '        sha=$(sed -n 1p "$run/$1")\n'
            "    else\n"
            "        sha=$(awk -v r=\"$1\" '$2 == r {print $1; exit}' "
            '"$run/packed-refs" 2>/dev/null)\n'
            "    fi\n"
            '    case "$sha" in ""|*[!0-9a-f]*) return 65 ;; esac\n'
            '    test "${#sha}" -eq 40 || return 65\n'
            "    printf '%s\\n' \"$sha\"\n"
            "}\n"
            'links=$(find "$run/objects" "$run/refs" -type l -print -quit) '
            "|| exit 65\n"
            'test -z "$links" || exit 65\n'
            'pack_index=$(find "$run/objects/pack" -name "multi-pack-index*" '
            "-print -quit) || exit 65\n"
            'test -z "$pack_index" || exit 65\n'
            'test ! -e "$run/objects/info/alternates" || exit 65\n'
            'head=$(sed -n 1p "$run/HEAD")\n'
            'case "$head" in\n'
            '    "ref: refs/heads/"*) tip=$(resolve "${head#ref: }") || exit 65 ;;\n'
            "    *) tip=$head ;;\n"
            "esac\n"
            'case "$tip" in ""|*[!0-9a-f]*) exit 65 ;; esac\n'
            'test "${#tip}" -eq 40 || exit 65\n'
            'case "$1" in refs/remotes/origin/*) ;; *) exit 65 ;; esac\n'
            'base=$(resolve "$1") || exit 65\n'
            "scratch=$(mktemp -d) || exit 65\n"
            "trap 'rm -rf \"$scratch\"' EXIT\n"
            f"{GIT} GIT_CONFIG_GLOBAL=/dev/null {GIT_FLAGS} "
            'init -q --bare --template= "$scratch" || exit 65\n'
            f'{GIT} GIT_CONFIG_GLOBAL=/dev/null GIT_DIR="$scratch" '
            f'GIT_OBJECT_DIRECTORY="$run/objects" {GIT_FLAGS} '
            "-c protocol.allow=never -c diff.external= -c core.multiPackIndex=false "
            "--no-optional-locks "
            "format-patch --stdout --no-ext-diff --no-textconv --no-color "
            "--no-signature --no-attach --no-add-header --no-notes "
            '"$base..$tip" --\n'
        )
        fallback = (
            f"pushed=$({MIRROR_GIT} ls-remote --end-of-options origin {branch}) "
            '|| exit "$?"\n'
            'if test -z "$pushed"; then\n'
            f"    default=$({MIRROR_GIT} ls-remote --symref "
            '--end-of-options origin HEAD) || exit "$?"\n'
            "    base=$(printf '%s\\n' \"$default\" | "
            'awk \'$1 == "ref:" && $2 ~ /^refs\\/heads\\// && $3 == "HEAD" '
            '{sub(/^refs\\/heads\\//, "refs/remotes/origin/", $2); print $2}\')\n'
            '    test -n "$base" || exit 65\n'
            f"    for p in {paths}; do\n"
            '        test ! -L "$p" && test -d "$p" || exit 65\n'
            "    done\n"
            f"    for p in {internals}; do\n"
            '        test ! -L "$p" || exit 65\n'
            "    done\n"
            f"    perl -e {shlex.quote(INSPECTION_OUTPUT)} "
            f'/bin/sh -c {shlex.quote(patch)} -- "$base"\n'
            '    exit "$?"\n'
            "fi\n"
        )
    return mirror_command(
        repository,
        name,
        fallback
        + f"{MIRROR_GIT} -c transfer.fsckObjects=true -c core.logAllRefUpdates=false "
        "fetch --no-write-fetch-head --no-tags --no-prune --no-prune-tags "
        f"--end-of-options origin +HEAD:{base} +{branch}:{branch} "
        '|| exit "$?"\n'
        f"perl -e {shlex.quote(INSPECTION_OUTPUT)} /bin/sh -c {shlex.quote(read)}",
    )


def clean(
    repository: str,
    name: str,
    stamp: str,
    sockets_dir: str,
    *,
    force: bool,
) -> str:
    clone = repo.clone_path(PurePosixPath("."), repository)
    tree = f".worktrees/agent-{name}"
    move = """check_directory() {
    if test -L "$1"; then
        printf '%s\\n' "clean refused: $1 is a symlink" >&2; exit 65
    fi
    if test -e "$1" && test ! -d "$1"; then
        printf '%s\\n' "clean refused: $1 is not a directory" >&2; exit 65
    fi
}
"""
    for component in (clone, clone / ".worktrees", clone / tree):
        move += f'check_directory "$HOME"/{shlex.quote(str(component))}\n'
    move += f"""path="$HOME"/{shlex.quote(str(clone / tree))}
if test -d "$path"; then
    test ! -e "$trash/worktree" && test ! -L "$trash/worktree" || exit 65
    mv "$path" "$trash/worktree" || exit "$?"
fi
"""
    command = (
        owned(name)
        + FILE_HELPERS
        + socket_root(sockets_dir)
        + f"""tmux_state has-session ag-{name}
test "$present" -eq 0 || exit 1
"""
    )
    if not force:
        command += f"""fallback="$d/{name}{files.FALLBACK}"
if test -e "$fallback" || test -L "$fallback"; then
    printf '%s\\n' 'clean refused: fallback rc requires --force' >&2
    exit 65
fi
"""
    command += (
        f"""trash="$d/trash/{stamp}-{name}"
test ! -L "$d/trash" && test ! -L "$trash" || exit 65
mkdir -p "$trash" || exit "$?"
"""
        + move
    )
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

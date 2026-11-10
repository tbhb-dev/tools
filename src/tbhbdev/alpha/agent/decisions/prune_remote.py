"""Owner-gated inventory script; no run contents or repository commands."""

from __future__ import annotations

import shlex
from pathlib import PurePosixPath

from tbhbdev.alpha.agent.decisions import remote, repo
from tbhbdev.alpha.agent.decisions import runfiles_names as files

MEASURE = r"""use strict;
use warnings;
use File::Find;
my ($rc, $keep, $tree, @paths) = @ARGV;
my @rc = lstat($rc);
my @keep = lstat($keep);
my ($count, $bytes) = (0, 0);
my $measure = sub {
    my @s = lstat($_[0]);
    die "inventory failed: $!" unless @s;
    if (-f _ || -l _) { $count++; $bytes += $s[7]; }
};
for my $p (@paths) { $measure->($p) if -e $p; }
if (-d $tree) {
    find({wanted => sub { $measure->($File::Find::name) },
          follow => 0, no_chdir => 1}, $tree);
}
printf "%s\n%d\n%d\n%d\n", @rc ? $rc[9] : '-', scalar(@keep) ? 1 : 0,
    $count, $bytes;
"""

UNMEASURED = r"""use strict;
use warnings;
my ($rc, $keep) = @ARGV;
my @rc = lstat($rc);
printf "%s\n%d\n-\n-\n", @rc ? $rc[9] : '-', -e $keep ? 1 : 0;
"""


def facts(
    repository: str, name: str, sockets_dir: str, *, owner: str, measure: bool
) -> str:
    clone = repo.clone_path(PurePosixPath("."), repository)
    tree = clone / f".worktrees/agent-{name}"
    command = remote.owned(name, owner) + remote.socket_root(sockets_dir)
    for suffix in files.SUFFIXES:
        command += f'safe_file "$d/{name}{suffix}"\n'
    for component in (*reversed(clone.parents[:-1]), clone, tree.parent, tree):
        command += (
            f'p="$HOME"/{shlex.quote(str(component))}\n'
            'test ! -L "$p" || exit 65\n'
            'if test -e "$p"; then test -d "$p" || exit 65; fi\n'
        )
    paths = " ".join(f'"$d/{name}{suffix}"' for suffix in files.SUFFIXES)
    return command + (
        f"tmux_state has-session ag-{name}\n"
        "printf '%s\\n' \"$present\"\n"
        f"perl -e {shlex.quote(MEASURE if measure else UNMEASURED)} "
        f'"$d/{name}{files.RC}" "$d/{name}{files.KEEP}" '
        f'"$HOME"/{shlex.quote(str(tree))} {paths}\n'
    )

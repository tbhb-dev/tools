"""The owner-gated command for an interactive tmux client."""

from __future__ import annotations

import shlex

from tbhbdev.alpha.agent import transport
from tbhbdev.alpha.agent.decisions import remote


def command(
    name: str, sockets_dir: str, *, owner: str, read_only: bool
) -> tuple[str, ...]:
    message = shlex.quote(
        transport.terminal_text(f"{name}: no session; the run has ended")
    )
    return (
        "/bin/sh",
        "-c",
        remote.owned(name, owner)
        + remote.socket_root(sockets_dir)
        + f"tmux_state has-session ag-{name}\n"
        + f"if test \"$present\" -eq 0; then printf '%s\\n' {message} >&2; exit 1; fi\n"
        + f'tmux -S "$s/agents" -f /dev/null attach-session -t "=ag-{name}"'
        + (" -r" if read_only else ""),
    )

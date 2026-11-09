# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns
"""`tbhbdev alpha session-context`: tell the model which host it runs on.

A Claude Code SessionStart hook, which fires on startup, resume, clear and
compact. Claude Code adds its stdout to the model's context. It prints the
host name and its role, the other hosts, the operating system and the local
time. The hosts come from `[harness] hosts` in the user config, a list of
`"name=role"` strings (see tbhbdev.userconfig). Without that key, the hook
prints nothing, and it always exits 0.
"""

import getpass
import platform
import socket
import sys
from datetime import datetime

from tbhbdev import userconfig

TABLE = "harness"


def os_name() -> str:
    """The operating system's name and version."""
    system = platform.system()
    if system == "Darwin":
        return f"macOS {platform.mac_ver()[0]}"
    if system == "Linux":
        try:
            return f"Linux {platform.freedesktop_os_release().get('PRETTY_NAME', '')}"
        except OSError:
            return "Linux"
    return f"{system} {platform.release()}"


def parse_hosts(entries: list[str]) -> dict[str, str]:
    """The `name=role` ENTRIES as a name-to-role dict; a bare name has no role."""
    hosts: dict[str, str] = {}
    for entry in entries:
        name, _, role = entry.partition("=")
        hosts[name.strip()] = role.strip()
    return hosts


def context(hosts: dict[str, str], host: str, now: datetime) -> str:
    """The context text for a session on HOST at NOW."""
    role = hosts.get(host)
    others = ", ".join(name for name in hosts if name != host)
    lines = [
        f"Session host: this session runs on {host}"
        + (f" ({role})" if role else "")
        + f", {os_name()}, as user {getpass.getuser()}."
    ]
    if others:
        lines.append(
            f"Other hosts: {others}. Reach them only over ssh; "
            f'"this machine" and "local" mean {host}.'
        )
    lines.append(f"Local time at session start: {now:%Y-%m-%d %H:%M %Z}.")
    return "\n".join(lines)


def main() -> int:
    """Print the session context; a missing config or any failure prints nothing."""
    try:
        entries = userconfig.optional_strings(TABLE, "hosts")
        if entries:
            host = socket.gethostname().split(".")[0]
            print(context(parse_hosts(entries), host, datetime.now().astimezone()))
    except Exception as exc:  # noqa: BLE001  # a broken hook must not stop a session
        print(f"session-context: {exc}", file=sys.stderr)
    return 0

"""The `tbhbdev` command: one subcommand per tool.

Each tool is a module of this package with a `main(argv, prog)` that returns
its exit status. `tbhbdev <tool> ARGS...` passes ARGS to it unchanged, with
`tbhbdev <tool>` as the program name in its messages. Plugins add tools of
their own (see plugins.py).
"""

import sys
from collections.abc import Callable, Mapping, Sequence

from tbhbdev import alpha, host, plugins, secret

Tool = Callable[[list[str], str], int]

# The built-in tools. main adds the plugins' tools when it runs.
TOOLS: dict[str, Tool] = {
    "alpha": alpha.main,
    "host": host.main,
    "secret": secret.main,
}


def usage(tools: Mapping[str, Tool]) -> str:
    return "usage: tbhbdev <tool> [args...]\n\ntools:\n" + "".join(
        f"  {name}\n" for name in sorted(tools)
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Run the tool argv names, passing it the rest of argv."""
    args = list(sys.argv[1:] if argv is None else argv)
    try:
        tools = plugins.tools(TOOLS)
    except plugins.PluginError as exc:
        sys.stderr.write(f"tbhbdev: {exc}\n")
        return 2
    if not args or args[0] in {"-h", "--help"}:
        stream = sys.stdout if args else sys.stderr
        stream.write(usage(tools))
        return 0 if args else 2
    name, rest = args[0], args[1:]
    tool = tools.get(name)
    if tool is None:
        sys.stderr.write(f"tbhbdev: unknown tool {name!r}\n{usage(tools)}")
        return 2
    return tool(rest, f"tbhbdev {name}")

"""Executable transport fakes; all state belongs to the test directory."""

from __future__ import annotations

import json
import os
import shlex
import sys
from dataclasses import dataclass
from typing import TYPE_CHECKING

from fake_tool import write_fake

from tbhbdev import version
from tbhbdev.alpha.agent.decisions import argv

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

BODY = """\
import base64
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path
root = Path(os.environ["EXAMPLE_REMOTE"])
control = json.loads((root / "control.json").read_text())
name = Path(sys.argv[0]).stem
args = sys.argv[1:]
data = sys.stdin.buffer.read() if name == "ssh" else b""
with (root / (name + ".jsonl")).open("a") as log:
    record = {"args": args, "stdin": base64.b64encode(data).decode()}
    log.write(json.dumps(record) + "\\n")
if name == "ssh":
    if len((root / "ssh.jsonl").read_text().splitlines()) > 20:
        raise AssertionError("ssh call budget exceeded")
    plan = control.get("ssh", [])
    if plan:
        item = plan.pop(0)
        (root / "control.json").write_text(json.dumps(control))
        if item is not None:
            output = item.get("out", "")
            if output.count("\\n") >= 4:
                fields = output.split("\\n", 4)
                fields[0] = (fields[0] or "unknown").encode().hex()
                fields[1] = fields[1].encode().hex() if fields[1] else "-"
                output = "\\n".join(fields)
            print(output, end="")
            sys.exit(item.get("rc", 0))
    command = shlex.split(args[-1])
    assert command[:2] in (["/bin/bash", "-c"], ["/bin/zsh", "-c"])
    env = {"PATH": os.environ["PATH"], "EXAMPLE_REMOTE": str(root),
           "HOME": str(root / "home"), "XDG_STATE_HOME": str(root / "state")}
    script = command[2].replace(
        "PATH=/usr/bin:/bin:/usr/sbin:/sbin", "PATH=" + os.environ["PATH"])
    script = script.replace("/tmp/example-sockets", str(root / "sockets"))
    result = subprocess.run(["/bin/sh", "-c", script], input=data, env=env)
    sys.exit(result.returncode)
if name == "tbhbdev":
    assert args == ["--version"]
    print(control["version"])
elif name == "tmux":
    assert args[:2] == ["-S", str(root / "sockets/agents")]
    verb = args[2]
    if verb in ("has-session", "kill-session"):
        assert args[-2:] == ["-t", "=ag-sample"]
    if verb == "has-session":
        if control.get("tmux_error"):
            sys.stderr.write(control["tmux_error"])
            sys.exit(1)
        if not control.get("session", False):
            sys.stderr.write("can't find session: ag-sample")
            sys.exit(1)
        sys.exit(0)
    run = root / "state/tbhbdev/agent-runs"
    if verb == "new-session":
        control["session"] = True
        launch = control.get("launch", {"launched": ""})
        for suffix, text in launch.items():
            (run / ("sample." + suffix)).write_text(text)
        if "rc" in launch:
            control["session"] = False
    elif verb == "kill-session":
        if not control.get("session", False):
            sys.stderr.write("can't find session: ag-sample")
            sys.exit(1)
        control["session"] = False
        if control.get("kill_rc") is not None:
            (run / "sample.rc").write_text(control["kill_rc"])
    else:
        raise AssertionError(args)
    (root / "control.json").write_text(json.dumps(control))
elif name == "git":
    assert args[:2] == ["-c", "core.hooksPath=/dev/null"]
    assert args[2:14:2] == ["-c"] * 6
    verb = args[14]
    assert Path.cwd() == (root / "home" if verb == "ls-remote" else
                          root / "home/Code/github.com/example-org/example-repo")
    if verb == "ls-remote":
        print(control.get("refs", ""), end="")
    elif verb == "show-ref":
        assert args[-1] == "refs/heads/agent/sample"
        sys.exit(0 if control.get("branch", False) else 1)
    elif verb == "rev-parse":
        if control.get("upstream", True):
            print("refs/remotes/origin/main")
        else:
            sys.exit(1)
    elif verb == "rev-list":
        print(control.get("unpushed", 0))
    elif verb == "worktree":
        sys.exit(control.get("worktree_rc", 0))
    elif verb != "branch":
        raise AssertionError(args)
else:
    raise AssertionError(name)
"""


@dataclass
class Remote:
    root: Path
    state: Path
    local: Path
    prompt: Path
    bin: Path

    def configure(self, **changes: object) -> None:
        path = self.root / "control.json"
        values = json.loads(path.read_text())
        values.update(changes)
        path.write_text(json.dumps(values))

    def records(self, tool: str) -> list[dict[str, object]]:
        path = self.root / f"{tool}.jsonl"
        return (
            [json.loads(line) for line in path.read_text().splitlines()]
            if path.exists()
            else []
        )

    def file(self, suffix: str, text: str) -> None:
        (self.state / ("sample" + suffix)).write_text(text)


def setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Remote:
    root = tmp_path / "remote"
    root.mkdir()
    state = root / "state/tbhbdev/agent-runs"
    state.mkdir(parents=True)
    (
        root / "home/Code/github.com/example-org/example-repo/.worktrees/agent-sample"
    ).mkdir(parents=True)
    tools = tmp_path / "bin"
    tools.mkdir()
    for name in ("ssh", "tmux", "git", "tbhbdev"):
        write_standalone_fake(
            tools / name,
            BODY.replace(
                'root = Path(os.environ["EXAMPLE_REMOTE"])',
                f"root = Path({str(root)!r})",
            ),
        )
    program = root / "home/.local/bin/tbhbdev"
    program.parent.mkdir(parents=True)
    program.symlink_to(tools / "tbhbdev")

    def executable(name: str) -> str:
        return str(tools / name)

    monkeypatch.setattr(argv, "executable", executable)
    (root / "control.json").write_text(json.dumps({"version": version.line()}))
    monkeypatch.setenv("EXAMPLE_REMOTE", str(root))
    monkeypatch.setenv("PATH", str(tools) + os.pathsep + os.environ["PATH"])
    local = tmp_path / "local-state"
    monkeypatch.setenv("XDG_STATE_HOME", str(local))
    prompt = tmp_path / "brief.md"
    prompt.write_text("Carry out the example.\n")
    return Remote(root, state, local, prompt, tools)


def write_standalone_fake(script: Path, body: str) -> None:
    """A standard-library-only fake, without site hooks that replay side effects."""
    write_fake(script, body)
    interpreter = shlex.quote(sys.executable)
    script.write_text(script.read_text().replace(interpreter, interpreter + " -S"))

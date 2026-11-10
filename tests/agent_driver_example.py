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
            print(item.get("out", ""), end="")
            sys.exit(item.get("rc", 0))
    command = shlex.split(args[-1])
    assert command[:2] in (["bash", "-lc"], ["zsh", "-lc"])
    env = {"PATH": os.environ["PATH"], "EXAMPLE_REMOTE": str(root),
           "HOME": str(root / "home"), "XDG_STATE_HOME": str(root / "state")}
    result = subprocess.run(["sh", "-c", command[2]], input=data, env=env)
    sys.exit(result.returncode)
if name == "tbhbdev":
    assert args == ["--version"]
    print(control["version"])
elif name == "tmux":
    assert args[:2] == ["-L", "agents"]
    verb = args[2]
    if verb in ("has-session", "kill-session"):
        assert args[-2:] == ["-t", "ag-sample"]
    if verb == "has-session":
        sys.exit(0 if control.get("session", False) else 1)
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
            sys.exit(1)
        control["session"] = False
        if control.get("kill_rc") is not None:
            (run / "sample.rc").write_text(control["kill_rc"])
    else:
        raise AssertionError(args)
    (root / "control.json").write_text(json.dumps(control))
elif name == "git":
    assert args[:2] == ["-c", "core.hooksPath=/dev/null"]
    verb = args[2]
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
        write_standalone_fake(tools / name, BODY)
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

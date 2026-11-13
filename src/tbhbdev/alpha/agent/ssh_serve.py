"""A bounded, forced-command SSH interface to one host run."""

from __future__ import annotations

import json
import os
import pwd
import shlex
import sys
from pathlib import Path

from tbhbdev.alpha.agent import build_info, ownership, process_group, run_host, runfiles
from tbhbdev.alpha.agent.decisions.run_state import Group, owns
from tbhbdev.alpha.agent.model import NAME, Invalid

MACOS_LAUNCH_KEY = Path("/Library/Application Support/tbhbdev/agent-launch/id_ed25519")
LINUX_LAUNCH_KEY = Path("/etc/tbhbdev/agent-launch/id_ed25519")
INPUT_LIMIT = 1024 * 1024
COMMAND_WORDS = 2


def command(arguments: list[str]) -> tuple[str, str]:
    if arguments:
        message = "ssh-serve takes no arguments"
        raise Invalid(message)
    if pwd.getpwuid(os.getuid()).pw_name != "agent":
        message = "requires agent account"
        raise Invalid(message)
    words = shlex.split(os.environ["SSH_ORIGINAL_COMMAND"])
    if len(words) != COMMAND_WORDS or words[0] not in (
        "run-host",
        "result",
        "kill",
    ):
        message = "expected run-host NAME, result NAME or kill NAME"
        raise Invalid(message)
    if not NAME.fullmatch(words[1]):
        message = "invalid run name"
        raise Invalid(message)
    return words[0], words[1]


def envelope() -> tuple[dict[str, object], str | None]:
    raw = sys.stdin.buffer.read(INPUT_LIMIT + 1)
    if len(raw) > INPUT_LIMIT:
        message = "stdin exceeds 1 MiB"
        raise Invalid(message)
    data: object = json.loads(raw)
    if not isinstance(data, dict) or data.keys() != {"v", "request", "push_token"}:
        message = "expected v, request and push_token"
        raise Invalid(message)
    if type(data["v"]) is not int or data["v"] != 1:
        message = "unsupported envelope version"
        raise Invalid(message)
    fields: dict[str, object] = data
    request = fields["request"]
    token = fields["push_token"]
    if not isinstance(request, dict):
        message = "request must be an object"
        raise Invalid(message)
    if token is not None and (
        not isinstance(token, str) or not token or any(c in token for c in "\r\n\0")
    ):
        message = "push_token must be a nonempty single line or null"
        raise Invalid(message)
    request_fields: dict[str, object] = request
    return request_fields, token


def start(files: runfiles.RunFiles, prog: str) -> int:
    request, token = envelope()
    run_host.refuse_started(files)
    ownership.claim(files)
    # Exclusive creation preserves the request of an existing run.
    fd = os.open(
        files.path("request.json"), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
    )
    with os.fdopen(fd, "w") as stream:
        stream.write(json.dumps(request) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    return run_host.main(
        [files.name], f"{prog} run-host", push_token=token, claimed=True
    )


def optional(path: Path) -> str | None:
    try:
        return path.read_text()
    except FileNotFoundError:
        return None


def result(files: runfiles.RunFiles) -> int:
    rc = optional(files.path("rc"))
    log = optional(files.path("log")) or ""
    print(
        json.dumps(
            {
                **build_info.identities(),
                "v": 1,
                "name": files.name,
                "rc": None if rc is None else int(rc),
                "state": optional(files.path("phase")),
                "report": optional(files.path("out.final")),
                "log_tail": "".join(log.splitlines(keepends=True)[-200:]),
                "branch": optional(files.path("branch")),
                "head": optional(files.path("head")),
            }
        )
    )
    return 0


def kill(files: runfiles.RunFiles) -> int:
    if files.path("rc").exists():
        print("already finished")
        return 0
    data = json.loads(files.path("pgid").read_text())
    recorded = Group(**data)
    if not owns(recorded, process_group.identity(recorded.pgid)):
        message = "run group identity no longer matches"
        raise Invalid(message)
    # TERM reaches the owned harness group; its supervisor records completion.
    if not process_group.stop_owned(files, recorded.pgid, 0):
        message = "cannot signal run group"
        raise Invalid(message)
    return 0


def main(arguments: list[str], prog: str) -> int:
    try:
        verb, name = command(arguments)
        files = runfiles.RunFiles(
            runfiles.state_home(Path.home()) / "tbhbdev" / "agent-runs", name
        )
        if verb == "run-host":
            return start(files, prog)
        if not files.path("request.json").exists():
            return 3
        return result(files) if verb == "result" else kill(files)
    except Invalid as exc:
        print(f"ssh-serve: refused: {exc}", file=sys.stderr)
        return 2
    except OSError, ValueError, TypeError, KeyError:
        # Never echo untrusted JSON or an exception that might contain its token.
        print("ssh-serve: refused: invalid command or run data", file=sys.stderr)
        return 2

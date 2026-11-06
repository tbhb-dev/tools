"""Stub executables for the tests of secrets and agent hosts.

conftest.py installs one small launcher per tool on PATH, each calling
`main` here with the tool's name. Every call appends {"tool", "argv"} (and
"stdin" for the tools that read it) to $STUB_DIR/calls.jsonl, and scenario
state sits in $STUB_DIR/state.json. Keychains live under $HOME, which the
tests and the sudo stub point into tmp_path, so nothing touches a real
keychain, 1Password account, host or tailnet.
"""

import json
import os
import shlex
import sys
from pathlib import Path
from typing import NoReturn, cast

type Json = dict[str, object]

ACCOUNT = "example.1password.com"
READS_STDIN = frozenset({"security", "secret-tool", "swift", "gnome-keyring-daemon"})


def stub_dir() -> Path:
    return Path(os.environ["STUB_DIR"])


def obj(value: object) -> Json:
    assert isinstance(value, dict)
    return cast("Json", value)


def rows(value: object) -> list[Json]:
    """VALUE, a JSON array of objects, itself: appending to it changes the state."""
    assert isinstance(value, list)
    assert all(isinstance(entry, dict) for entry in cast("list[object]", value))
    return cast("list[Json]", value)


def load() -> Json:
    path = stub_dir() / "state.json"
    return obj(json.loads(path.read_text())) if path.exists() else {}


def save(state: Json) -> None:
    (stub_dir() / "state.json").write_text(json.dumps(state, indent=1))


def log(tool: str, argv: list[str], stdin: str | None = None) -> None:
    entry: Json = {"tool": tool, "argv": argv}
    if stdin is not None:
        entry["stdin"] = stdin
    with (stub_dir() / "calls.jsonl").open("a") as out:
        out.write(json.dumps(entry) + "\n")


def fail(message: str, code: int = 1) -> NoReturn:
    sys.stderr.write(message + "\n")
    sys.exit(code)


def flag(args: list[str], name: str) -> str:
    return args[args.index(name) + 1] if name in args else ""


def flags(args: list[str], name: str) -> list[str]:
    return [args[i + 1] for i, arg in enumerate(args) if arg == name]


# ssh, sudo and dbus-run-session: run the command here.


def ssh(args: list[str]) -> None:
    while args and args[0] == "-o":
        args = args[2:]
    host, command = args[0], " ".join(args[1:])
    if host in cast("list[str]", load().get("ssh_down", [])):
        fail(f"ssh: connect to host {host}: Network is unreachable", 255)
    os.environ["STUB_HOST"] = host
    os.execvp("sh", ["sh", "-c", command])  # noqa: S606


def sudo(args: list[str]) -> None:
    if args[:4] != ["-n", "-u", "agent", "-H"]:
        fail(f"sudo stub: unexpected options {args[:4]}")
    home = stub_dir() / "homes" / os.environ.get("STUB_HOST", "local") / "agent"
    home.mkdir(parents=True, exist_ok=True)
    os.environ["HOME"] = str(home)
    # sudo resets the environment, so the agent has no XDG_CONFIG_HOME.
    os.environ.pop("XDG_CONFIG_HOME", None)
    os.execvp(args[4], args[4:])  # noqa: S606


def dbus_run_session(args: list[str]) -> None:
    if args[0] != "--":
        fail(f"dbus-run-session stub: unexpected options {args[:1]}")
    os.environ["STUB_DBUS"] = "1"
    os.execvp(args[1], args[1:])  # noqa: S606


# The macOS keychains: one JSON file per keychain under $HOME.


def keychain_file(name: str) -> Path:
    return Path(os.environ["HOME"]) / "Library" / "Keychains" / f"{name}-db"


def read_keychain(name: str) -> Json:
    path = keychain_file(name)
    if not path.exists():
        fail(f"security: {name}: The specified keychain could not be found.", 50)
    return obj(json.loads(path.read_text()))


def item_key(args: list[str]) -> str:
    return f"{flag(args, '-s')}/{flag(args, '-a')}"


def security_interactive(args: list[str]) -> None:
    commands = sys.stdin.read()
    log("security", args, commands)
    if load().get("security_i_broken"):
        return
    for line in commands.splitlines():
        words = shlex.split(line)
        if words[0] == "add-generic-password":
            data = read_keychain(words[-1])
            obj(data["items"])[item_key(words)] = flag(words, "-w")
            keychain_file(words[-1]).write_text(json.dumps(data))


def security_item(verb: str, rest: list[str], data: Json) -> None:
    items = obj(data["items"])
    key = item_key(rest)
    if key not in items:
        fail("security: The specified item could not be found in the keychain.", 44)
    if verb == "delete-generic-password":
        del items[key]
    elif "-w" in rest:
        sys.stdout.write(f"{items[key]}\n")


def security(args: list[str]) -> None:
    verb, rest = args[0], args[1:]
    if verb == "-i":
        security_interactive(args)
        return
    log("security", args)
    name = rest[-1]
    if verb == "create-keychain":
        path = keychain_file(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        empty: Json = {}
        data: Json = {"password": flag(rest, "-p"), "locked": True, "items": empty}
        path.write_text(json.dumps(data))
        return
    data = read_keychain(name)
    if verb == "unlock-keychain":
        data["locked"] = flag(rest, "-p") != data["password"]
    elif verb == "set-keychain-settings":
        data["settings"] = rest[:-1]
    elif data["locked"]:
        fail("security: User interaction is not allowed.", 36)
    else:
        security_item(verb, rest, data)
    keychain_file(name).write_text(json.dumps(data))


# The GNOME Keyring: one JSON file under $HOME, reachable only inside
# dbus-run-session once gnome-keyring-daemon has unlocked it.


def keyring_file() -> Path:
    return Path(os.environ["HOME"]) / ".local" / "share" / "keyrings" / "stub.json"


def gnome_keyring_daemon(args: list[str]) -> None:
    password = sys.stdin.read()
    log("gnome-keyring-daemon", args, password)
    if password or args != ["--unlock", "--components=secrets"]:
        fail("gnome-keyring-daemon stub: unexpected unlock")
    path = keyring_file()
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}")


def secret_tool(args: list[str]) -> None:
    if not os.environ.get("STUB_DBUS") or not keyring_file().exists():
        log("secret-tool", args)
        fail("secret-tool: Cannot autolaunch D-Bus without X11 $DISPLAY")
    data = obj(json.loads(keyring_file().read_text()))
    key = " ".join(arg for arg in args[1:] if not arg.startswith("--"))
    if args[0] == "store":
        value = sys.stdin.read()
        log("secret-tool", args, value)
        data[key] = value
        keyring_file().write_text(json.dumps(data))
        return
    log("secret-tool", args)
    if key not in data:
        sys.exit(1)
    sys.stdout.write(str(data[key]))
    sys.exit(cast("int", load().get("secret_tool_rc", 0)))


# op: a 1Password account with Connect servers, vaults and tokens.


def op_publish(tokens: list[Json]) -> None:
    """Tell the fake Connect API which live tokens reach which vaults."""
    live = {str(t["token"]): t["vaults"] for t in tokens if t["state"] == "ACTIVE"}
    (stub_dir() / "connect-tokens.json").write_text(json.dumps(live))


def op_token_create(account: Json, args: list[str]) -> None:
    vaults = flags(args, "--vault")
    granted = cast("list[str]", account["grants"])
    for vault in vaults:
        if vault not in granted:
            fail(f"[ERROR] the server has no access to the vault {vault}")
    tokens = rows(account["tokens"])
    count = len(tokens) + 1
    value = f"fake-connect-token-{count}"
    tokens.append(
        {
            "id": f"tok-{count}",
            "name": args[3],
            "state": "ACTIVE",
            "integrationId": flag(args, "--server"),
            "token": value,
            "vaults": vaults,
            "expires_in": flag(args, "--expires-in"),
        }
    )
    account["tokens"] = tokens
    sys.stdout.write(f"{load().get('op_token_output', value)}\n")


def op_list(account: Json, command: str, args: list[str]) -> None:
    key = command.split()[-2] + "s"
    entries = rows(account[key])
    if key == "tokens":
        entries = [e for e in entries if e["integrationId"] == flag(args, "--server")]
        keep = ("id", "name", "state", "integrationId")
    else:
        keep = ("id", "name")
    sys.stdout.write(json.dumps([{k: e[k] for k in keep} for e in entries]) + "\n")


def op_change(account: Json, command: str, args: list[str]) -> None:
    if command == "connect server create":
        Path("1password-credentials.json").write_text('{"stub": "credentials"}')
        rows(account["servers"]).append({"id": "srv-1", "name": args[3]})
    elif command == "vault create":
        vaults = rows(account["vaults"])
        vaults.append({"id": f"vlt-{len(vaults) + 1}", "name": args[2]})
    elif command == "connect vault grant":
        cast("list[str]", account["grants"]).append(flag(args, "--vault"))
    elif command == "connect vault revoke":
        cast("list[str]", account["grants"]).remove(flag(args, "--vault"))
    elif command == "connect token create":
        op_token_create(account, args)
    elif command == "connect token delete":
        for entry in rows(account["tokens"]):
            if entry["id"] == args[3]:
                entry["state"] = "REVOKED"
    else:
        fail(f"op stub: unknown command {command!r}")


def op(args: list[str]) -> None:
    log("op", args)
    if flag(args, "--account") != ACCOUNT:
        fail("[ERROR] no account given; the default account is the wrong one", 9)
    command = " ".join(args[:3] if args[0] == "connect" else args[:2])
    state = load()
    failures = obj(state.get("op_fail", {}))
    if command in failures:
        fail(f"[ERROR] {failures[command]}")
    raw = obj(state.get("op_raw", {}))
    if command in raw:
        sys.stdout.write(f"{raw[command]}\n")
        return
    empty: Json = {"servers": [], "vaults": [], "grants": [], "tokens": []}
    account = obj(state.setdefault("op", empty))
    servers = [s["id"] for s in rows(account["servers"])]
    if command.startswith(("connect vault", "connect token")) and (
        flag(args, "--server") not in servers
    ):
        fail(f"[ERROR] no Connect server {flag(args, '--server')!r}")
    if command.endswith(" list"):
        op_list(account, command, args)
        return
    op_change(account, command, args)
    save(state)
    op_publish(rows(account["tokens"]))


def tailscale(args: list[str]) -> None:
    state = load()
    if args != ["status", "--json"] or "tailscale_error" in state:
        fail(f"tailscale: {state.get('tailscale_error', 'unexpected arguments')}")
    status = state["tailscale"]
    sys.stdout.write((status if isinstance(status, str) else json.dumps(status)) + "\n")


def swift(args: list[str]) -> None:
    script = sys.stdin.read()
    log("swift", args, script)
    sys.exit(cast("int", load().get("swift_rc", 0)))


TOOLS = {
    "dbus-run-session": dbus_run_session,
    "gnome-keyring-daemon": gnome_keyring_daemon,
    "op": op,
    "secret-tool": secret_tool,
    "security": security,
    "ssh": ssh,
    "sudo": sudo,
    "swift": swift,
    "tailscale": tailscale,
}


def main(tool: str, argv: list[str]) -> None:
    if tool not in READS_STDIN and tool != "op":
        log(tool, argv)
    TOOLS[tool](argv)

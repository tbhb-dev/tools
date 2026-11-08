"""Stub executables for the tests of secrets and agent hosts.

conftest.py installs one small launcher per tool on PATH, each calling
`main` here with the tool's name. Every call appends {"tool", "argv"} (and
"stdin" for the tools that read it) to $STUB_DIR/calls.jsonl, and scenario
state sits in $STUB_DIR/state.json. Keychains live under $HOME, which the
tests and the sudo stub point into tmp_path, so nothing touches a real
keychain, 1Password account, host or tailnet.

Each host's accounts are records in the state, which dscl, getent and the
shadow tools share. ACLs are state entries too, keyed by path. The tools that
change accounts, homes or ACLs run only under the sudo stub's root mode and
only on paths in the test's scratch directory, so nothing touches a real
account or ACL either. git and gh keep a clone's state in its `.git` file.
"""

import functools
import hashlib
import json
import os
import shlex
import stat
import sys
from pathlib import Path
from typing import NoReturn, cast

type Json = dict[str, object]

ACCOUNT = "example.1password.com"
READS_STDIN = frozenset({"security", "swift"})


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


# ssh and sudo: run the command here.


def host_key() -> str:
    return os.environ.get("STUB_HOST", "local")


def ssh(args: list[str]) -> None:
    while args and args[0] in {"-o", "-t"}:
        if args[0] == "-t":
            os.environ["STUB_TTY"] = "1"
        args = args[2:] if args[0] == "-o" else args[1:]
    host, command = args[0], " ".join(args[1:])
    if host in cast("list[str]", load().get("ssh_down", [])):
        fail(f"ssh: connect to host {host}: Network is unreachable", 255)
    os.environ["STUB_HOST"] = host
    home = stub_dir() / "homes" / host / "operator"
    home.mkdir(parents=True, exist_ok=True)
    os.environ["HOME"] = str(home)
    os.execvp("sh", ["sh", "-c", command])  # noqa: S606


def as_root(argv: list[str]) -> NoReturn:
    os.environ["STUB_ROOT"] = "1"
    os.execvp(argv[0], argv)  # noqa: S606


def sudo(args: list[str]) -> None:
    """As the agent with -n -u agent -H, or as root with -n or a password."""
    if args[:4] == ["-n", "-u", "agent", "-H"]:
        home = stub_dir() / "homes" / host_key() / "agent"
        home.mkdir(parents=True, exist_ok=True)
        os.environ["HOME"] = str(home)
        os.environ["STUB_AGENT"] = "1"
        # sudo resets the environment, so the agent has no XDG_CONFIG_HOME.
        os.environ.pop("XDG_CONFIG_HOME", None)
        os.execvp(args[4], args[4:])  # noqa: S606
    state = load()
    if args[:1] == ["-n"]:
        if state.get("sudo_password"):
            fail("sudo: a password is required")
        as_root(args[1:])
    if args[:1] != ["sh"]:
        fail(f"sudo stub: unexpected options {args[:1]}")
    if host_key() != "local" and not os.environ.get("STUB_TTY"):
        fail("sudo: a terminal is required to read the password")
    log("sudo-prompt", [host_key()])
    if state.get("sudo_wrong_password"):
        fail("sudo: 3 incorrect password attempts")
    as_root(args)


def need_root(tool: str) -> None:
    if not os.environ.get("STUB_ROOT"):
        fail(f"{tool}: Permission denied")


def scratch(tool: str, path: str) -> Path:
    """PATH, which must sit in the test's scratch directory."""
    resolved = Path(path).resolve()
    if not resolved.is_relative_to(stub_dir().parent.resolve()):
        fail(f"{tool} stub: refusing {path}, which is outside the test")
    return resolved


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


LISTS = frozenset(
    {"connect server list", "connect token list", "vault list", "item list"}
)
# The item commands that take a JSON item on stdin.
ITEM_INPUT = frozenset({"item create", "item edit"})


def op_item(account: Json, command: str, args: list[str], stdin: str) -> None:
    """Items by vault name. The create and edit stdin is the whole item."""
    vaults = obj(account.setdefault("items", {}))
    items = rows(vaults.setdefault(flag(args, "--vault"), []))
    if command == "item list":
        out = [{"id": i["id"], "title": i["title"]} for i in items]
        sys.stdout.write(json.dumps(out) + "\n")
        return
    if command == "item create":
        if args[2] != "-":
            fail(f"op stub: create without the item on stdin: {args}")
        items.append({**obj(json.loads(stdin)), "id": f"itm-{len(items) + 1}"})
        return
    matches = [i for i in items if args[2] in (i["id"], i["title"])]
    if len(matches) != 1:
        fail(f"[ERROR] {len(matches)} items match {args[2]}")
    if command == "item edit":
        items[items.index(matches[0])] = {
            **obj(json.loads(stdin)),
            "id": matches[0]["id"],
        }
    elif "--reveal" not in args:
        fail("op stub: item get conceals the fields without --reveal")
    else:
        sys.stdout.write(json.dumps(matches[0]) + "\n")


ON_A_SERVER = frozenset(
    {
        "connect vault grant",
        "connect vault revoke",
        "connect token list",
        "connect token create",
        "connect token delete",
    }
)


def op(args: list[str]) -> None:
    command = " ".join(args[:3] if args[0] == "connect" else args[:2])
    stdin = sys.stdin.read() if command in ITEM_INPUT else None
    log("op", args, stdin)
    if flag(args, "--account") != ACCOUNT:
        fail("[ERROR] no account given; the default account is the wrong one", 9)
    if command.endswith(" list") and (
        command not in LISTS or args[-4:-2] != ["--format", "json"]
    ):
        fail(f"op stub: unexpected list {args}")
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
    if command in ON_A_SERVER and flag(args, "--server") not in servers:
        fail(f"[ERROR] {command} needs --server and a server id: {args}")
    if args[0] == "item":
        op_item(account, command, args, stdin or "")
    elif command.endswith(" list"):
        op_list(account, command, args)
        return
    else:
        op_change(account, command, args)
    save(state)
    op_publish(rows(account["tokens"]))


# Accounts: each host's users and groups as dscl-style records, which dscl,
# getent, groupadd, useradd and usermod share.


type Record = dict[str, list[str]]


def directory(state: Json) -> dict[str, dict[str, Record]]:
    hosts = obj(state.setdefault("directory", {}))
    empty: Json = {"Users": {}, "Groups": {}}
    return cast("dict[str, dict[str, Record]]", hosts.setdefault(host_key(), empty))


def dscl_read(records: dict[str, dict[str, Record]], kind: str, name: str) -> None:
    record = records[kind].get(name)
    if record is None:
        fail("<dscl_cmd> DS Error: -14136 (eDSRecordNotFound)", 56)
    if load().get("dscl_preamble"):
        sys.stdout.write(" a continuation line before any key\n")
    for key, values in record.items():
        if any(" " in v or "/" in v for v in values):
            sys.stdout.write(f"{key}:\n" + "".join(f" {v}\n" for v in values))
        else:
            sys.stdout.write(f"{key}: {' '.join(values)}\n")
    sys.stdout.write(f"RecordName: {name}\n")


def dscl(args: list[str]) -> None:
    state = load()
    verb, path, rest = args[1], args[2], args[3:]
    errors = obj(state.get("dscl_error", {}))
    if verb in errors:
        fail(f"<dscl_cmd> DS Error: {errors[verb]}", 70)
    records = directory(state)
    kind, name = [*path.strip("/").split("/"), ""][:2]
    if verb == "-read":
        dscl_read(records, kind, name)
        return
    if verb == "-list":
        for each, record in records[kind].items():
            if rest[0] in record:
                sys.stdout.write(f"{each}  {' '.join(record[rest[0]])}\n")
        return
    need_root("dscl")
    record = records[kind].setdefault(name, {})
    if verb == "-create" and rest:
        record[rest[0]] = rest[1:]
    elif verb == "-append":
        record.setdefault(rest[0], []).extend(rest[1:])
    save(state)


def getent(args: list[str]) -> None:
    state = load()
    if "getent_error" in state:
        fail(str(state["getent_error"]), 3)
    records = directory(state)
    database, key = args
    if database not in {"passwd", "group"}:
        fail(f"Unknown database: {database}", 1)
    kind, number = (
        ("Users", "UniqueID") if database == "passwd" else ("Groups", "PrimaryGroupID")
    )
    for name, record in records[kind].items():
        if key in {name, *record.get(number, [])}:
            break
    else:
        sys.exit(2)

    def one(attribute: str) -> str:
        return " ".join(record.get(attribute, []))

    if database == "passwd":
        fields = [
            name,
            "x",
            one("UniqueID"),
            one("PrimaryGroupID"),
            "",
            one("NFSHomeDirectory"),
            one("UserShell"),
        ]
    else:
        fields = [
            name,
            "x",
            one("PrimaryGroupID"),
            ",".join(record.get("GroupMembership", [])),
        ]
    sys.stdout.write(":".join(fields) + "\n")


def shadow(tool: str, args: list[str]) -> None:
    """groupadd, useradd and usermod, with the options bootstrap passes."""
    need_root(tool)
    state = load()
    records = directory(state)
    name = args[-1]
    if tool == "groupadd":
        if name in records["Groups"]:
            fail(f"groupadd: group '{name}' already exists", 9)
        records["Groups"][name] = {"PrimaryGroupID": [flag(args, "--gid")]}
    elif tool == "useradd":
        if name in records["Users"]:
            fail(f"useradd: user '{name}' already exists", 9)
        home = flag(args, "--home-dir")
        records["Users"][name] = {
            "UniqueID": [flag(args, "--uid")],
            "PrimaryGroupID": [flag(args, "--gid")],
            "NFSHomeDirectory": [home],
            "UserShell": [flag(args, "--shell")],
        }
        scratch(tool, home).mkdir(mode=0o755, parents=True, exist_ok=True)
    elif "--lock" in args:
        records["Users"][name]["Password"] = ["!"]
    else:
        group = records["Groups"][flag(args, "--groups")]
        group.setdefault("GroupMembership", []).append(name)
    save(state)


def id_(args: list[str]) -> None:
    if args != ["-un"]:
        fail(f"id stub: unexpected arguments {args}")
    state = load()
    operators = obj(state.get("operators", {}))
    sys.stdout.write(f"{operators.get(host_key(), 'operator')}\n")
    fails = state.get("id_fails")
    if fails is True or (isinstance(fails, list) and host_key() in fails):
        sys.exit(1)


def install(args: list[str]) -> None:
    need_root("install")
    path = scratch("install", args[-1])
    path.mkdir(parents=True, exist_ok=True)
    path.chmod(int(flag(args, "-m"), 8))
    state = load()
    owners = obj(state.setdefault("owners", {}))
    owners[str(path)] = f"{flag(args, '-o')}:{flag(args, '-g')}"
    save(state)


# ACLs: macOS's as a list of entries per path, so a second chmod +a of the
# same entry shows up as a duplicate, and Linux's as a map of tag to rights.


def acl_paths(state: Json, key: str) -> dict[str, list[str]]:
    return cast("dict[str, list[str]]", state.setdefault(key, {}))


def chmod(args: list[str]) -> None:
    """ACL changes as root; a plain mode change, on a file of the test's, as is."""
    if args[0] not in {"+a", "-a"}:
        scratch("chmod", args[-1])
        os.execv("/bin/chmod", ["chmod", *args])  # noqa: S606
    need_root("chmod")
    op_, entry, paths = args[0], args[1], args[2:]
    state = load()
    acls = acl_paths(state, "acls_mac")
    for path in paths:
        key = str(scratch("chmod", path))
        if op_ == "+a":
            acls.setdefault(key, []).append(entry)
        elif entry in acls.get(key, []):
            acls[key].remove(entry)
        else:
            fail(f"chmod: No ACL present '{path}'")
    save(state)


def ls(args: list[str]) -> None:
    state = load()
    if args[:1] != ["-led"]:
        if os.environ.get("STUB_AGENT") and state.get("probe_denied"):
            fail("ls: .: Permission denied")
        os.execv("/bin/ls", ["ls", *args])  # noqa: S606
    if "ls_error" in state:
        fail(f"ls: {state['ls_error']}")
    path = args[1]
    sys.stdout.write(f"drwx------+ 3 operator  staff  96 Nov  4 12:00 {path}\n")
    entries = acl_paths(state, "acls_mac").get(str(Path(path).resolve()), [])
    for number, entry in enumerate(entries):
        sys.stdout.write(f" {number}: {entry}\n")


def getfacl(args: list[str]) -> None:
    if args == ["--version"]:
        sys.stdout.write("getfacl 2.3.2\n")
        return
    state = load()
    if "getfacl_error" in state:
        fail(f"getfacl: {state['getfacl_error']}")
    entries = acl_paths(state, "acls_linux").get(str(Path(args[-1]).resolve()), [])
    sys.stdout.write("user::rwx\ngroup::---\n")
    for entry in entries:
        sys.stdout.write(
            f"{entry}\t#effective:{entry[-3:]}\n"
            if entry.startswith("group:")
            else f"{entry}\n"
        )
    sys.stdout.write("other::---\n\n")


def setfacl_one(acls: dict[str, list[str]], path: Path, spec: str) -> None:
    tag, _, rights = spec.rpartition(":")
    if tag.startswith("default:") and not path.is_dir():
        fail(f"setfacl: {path}: Only directories can have default ACLs")
    if "X" in rights:
        executable = path.is_dir() or path.stat().st_mode & stat.S_IXUSR
        rights = rights.replace("X", "x" if executable else "-")
    entries = [e for e in acls.get(str(path), []) if e.rpartition(":")[0] != tag]
    acls[str(path)] = [*entries, f"{tag}:{rights}"]


def setfacl(args: list[str]) -> None:
    if args == ["--version"]:
        sys.stdout.write("setfacl 2.3.2\n")
        return
    need_root("setfacl")
    spec, paths = flag(args, "-m"), args[args.index("-m") + 2 :]
    state = load()
    acls = acl_paths(state, "acls_linux")
    for path in (scratch("setfacl", p) for p in paths):
        for target in below(path) if "-R" in args else [path]:
            if not target.is_symlink():
                setfacl_one(acls, target, spec)
    save(state)


def below(path: Path) -> list[Path]:
    """PATH and everything below it, not following symbolic links."""
    found = [path]
    for top, dirs, files in os.walk(path):
        found += [Path(top, n) for n in [*dirs, *files]]
    return found


# git, gh and mise. A clone is a directory whose .git file holds its state:
# its repo, branch (null when detached), HEAD, origin's branches, and
# whether tracked files are modified. Remote repos are in state["remotes"].


def sha(label: str) -> str:
    return hashlib.sha1(label.encode()).hexdigest()  # noqa: S324


def remote(repo: str) -> Json:
    remotes = obj(load().get("remotes", {}))
    if repo not in remotes:
        fail(f"GraphQL: Could not resolve to a Repository with the name '{repo}'.")
    return obj(remotes[repo])


def gh_agent() -> None:
    wanted = str(Path(os.environ["HOME"]) / ".config" / "gh-agent")
    if os.environ.get("GH_CONFIG_DIR") != wanted:
        fail("gh stub: not the agent's gh config", 4)


def gh_repo_list(args: list[str]) -> None:
    """The operator's gh: each owner's repositories, all private."""
    if args[3:6] != ["--visibility", "private", "--limit"] or args[7:] != [
        "--json",
        "name",
    ]:
        fail(f"gh stub: unexpected arguments {args}")
    state, owner = load(), args[2]
    failures = obj(state.get("gh_fail", {}))
    if owner in failures:
        fail(str(failures[owner]))
    raw = obj(state.get("gh_raw", {}))
    repos = obj(state.get("gh_repos", {}))
    names = cast("list[str]", repos.get(owner, []))
    out = raw.get(owner, json.dumps([{"name": n} for n in names]))
    sys.stdout.write(f"{out}\n")


def gh(args: list[str]) -> None:
    if args[:2] == ["repo", "list"]:
        gh_repo_list(args)
        return
    gh_agent()
    if args[:2] != ["repo", "clone"] or args[4:] != ["--", "--quiet"]:
        fail(f"gh stub: unexpected arguments {args}")
    if load().get("clone_fail"):
        fail("fatal: unable to access the repository")
    repo, path = args[2], scratch("gh", args[3])
    upstream = remote(repo)
    default, history = str(upstream["default"]), cast("list[str]", upstream["history"])
    path.mkdir()
    clone: Json = {
        "repo": repo,
        "branch": default,
        "head": history[-1],
        "origin": {default: history[-1]},
        "dirty": False,
    }
    (path / ".git").write_text(json.dumps(clone))


def git_fetch(clone: Json, args: list[str]) -> None:
    gh_agent()
    if args[:4] != [
        "-c",
        "credential.helper=",
        "-c",
        "credential.helper=!gh auth git-credential",
    ]:
        fail("git stub: fetch without the agent's credential helper", 128)
    if clone["repo"] in cast("list[str]", load().get("fetch_fail", [])):
        fail("fatal: could not read from remote repository.", 128)
    upstream = remote(str(clone["repo"]))
    obj(clone["origin"])[str(upstream["default"])] = cast(
        "list[str]", upstream["history"]
    )[-1]


def upstreams(clone: Json) -> list[str]:
    return [f"origin/{b}" for b in cast("dict[str, str]", clone["origin"])]


def expect(args: list[str], *wanted: list[str]) -> None:
    if args not in wanted:
        fail(f"git stub: unexpected arguments {args}", 129)


def git_query(clone: Json, args: list[str]) -> None:
    if args[0] == "status":
        expect(args, ["status", "--porcelain", "--untracked-files=no"])
        if "status_broken" in clone:
            fail("fatal: index file corrupt", 128)
        sys.stdout.write(" M README.md\n" if clone["dirty"] else "")
    elif args[0] == "merge-base":
        expect(
            args,
            *(["merge-base", "--is-ancestor", "HEAD", u] for u in upstreams(clone)),
        )
        history = cast("list[str]", remote(str(clone["repo"]))["history"])
        sys.exit(0 if clone["head"] in history else 1)
    else:
        git_ref(clone, args)


def git_ref(clone: Json, args: list[str]) -> None:
    origin = cast("dict[str, str]", clone["origin"])
    if args == ["rev-parse", "--abbrev-ref", "origin/HEAD"]:
        if "no_origin_head" in clone:
            fail("fatal: ambiguous argument 'origin/HEAD'", 128)
        sys.stdout.write(
            f"{clone.get('origin_head', f'origin/{next(iter(origin))}')}\n"
        )
    elif args[0] == "symbolic-ref":
        expect(args, ["symbolic-ref", "--quiet", "--short", "HEAD"])
        if clone["branch"] is None:
            sys.exit(1)
        sys.stdout.write(f"{clone['branch']}\n")
    else:
        expect(args, *(["rev-parse", "HEAD", u] for u in upstreams(clone)))
        if "unresolved" in clone:
            fail("fatal: bad revision", 128)
        sys.stdout.write(
            f"{clone['head']}\n{origin[args[2].removeprefix('origin/')]}\n"
        )


def git(args: list[str]) -> None:
    path = scratch("git", args[1])
    if not (path / ".git").is_file():
        fail(f"fatal: not a git repository: {path}", 128)
    clone = obj(json.loads((path / ".git").read_text()))
    rest = args[2:]
    if "fetch" in rest:
        git_fetch(clone, rest)
    elif rest[0] == "merge":
        expect(rest, *(["merge", "--ff-only", "--quiet", u] for u in upstreams(clone)))
        if "merge_fail" in clone:
            fail("fatal: Not possible to fast-forward, aborting.", 128)
        clone["head"] = cast("dict[str, str]", clone["origin"])[
            rest[-1].removeprefix("origin/")
        ]
    else:
        git_query(clone, rest)
        return
    (path / ".git").write_text(json.dumps(clone))


def mise(args: list[str]) -> None:
    state = load()
    if args == ["install", "--dry-run-code"]:
        sys.exit(1 if state.get("mise_missing") else 0)
    if args != ["install"]:
        fail(f"mise stub: unexpected arguments {args}")
    if state.get("mise_broken"):
        fail("mise ERROR failed to install tools")
    state["mise_missing"] = False
    state["mise_cwd"] = str(Path.cwd())
    save(state)


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
    "chmod": chmod,
    "dscl": dscl,
    "getent": getent,
    "getfacl": getfacl,
    "gh": gh,
    "git": git,
    "groupadd": functools.partial(shadow, "groupadd"),
    "id": id_,
    "install": install,
    "ls": ls,
    "mise": mise,
    "op": op,
    "security": security,
    "setfacl": setfacl,
    "ssh": ssh,
    "sudo": sudo,
    "swift": swift,
    "tailscale": tailscale,
    "useradd": functools.partial(shadow, "useradd"),
    "usermod": functools.partial(shadow, "usermod"),
}


def main(tool: str, argv: list[str]) -> None:
    if tool not in READS_STDIN and tool != "op":
        log(tool, argv)
    TOOLS[tool](argv)

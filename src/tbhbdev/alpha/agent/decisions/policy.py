"""The sandbox policy of a run: what its harness may touch, and its environment.

`build` is a pure function from the settings, the host, the run's choices and
the run's paths to a frozen `Policy`. It reads nothing, checks no path on disk
and never receives a parent environment. Every path in the policy is absolute,
and a config path that isn't absolute resolves against the run account's home.
There are no default lists here: write roots, denied paths and `PATH` come from
`[agent.sandbox]`.
"""

from __future__ import annotations

from pathlib import PurePosixPath

from tbhbdev import secrets
from tbhbdev.alpha.agent.model import (
    AgyPolicy,
    ClaudePolicy,
    CodexMode,
    CodexPolicy,
    Harness,
    Host,
    Invalid,
    Os,
    Policy,
    Profile,
    RunPaths,
    Settings,
)

# Codex's name for the whole filesystem, which a profile makes read-only.
READ_ROOT = ":root"
APPROVAL_POLICY = "never"
LINUX_LANG = "C.UTF-8"
MACOS_LANG = "en_US.UTF-8"
# Process substitution and /dev/stderr need access to open descriptors on macOS.
_DEV_FD = "/dev/fd"
# The handoff directories, under the run's private TMPDIR. run-host creates
# each one, and the child gets its path in the variable of the same tool.
GH_CONFIG_SUBDIR = "gh"
DOCKER_CONFIG_SUBDIR = "docker"
# macOS's `/tmp` is a link to `/private/tmp`, so a denied path under it needs both.
_TMP = "/tmp"  # noqa: S108 # nosec B108 -- an alias, not a temp file
# Claude's file tools, which a review run doesn't get.
_CLAUDE_FILE_TOOLS = ("Edit", "Write", "NotebookEdit")


def resolve(home: str, path: str) -> str:
    """PATH as an absolute path, resolved against HOME unless it's absolute."""
    return str(PurePosixPath(home) / path)


def _check(
    host: Host, harness: Harness, profile: Profile, *, account: str, full: bool
) -> None:
    if account not in host.accounts:
        message = f"policy account {account!r} isn't one of the accounts of {host.name}"
        raise Invalid(message)
    if not full:
        return
    if harness is not Harness.CODEX:
        message = "policy unsandboxed applies only to a codex run"
        raise Invalid(message)
    if profile is not Profile.BUILD:
        message = "policy unsandboxed applies only to a build run"
        raise Invalid(message)
    if not host.unsandboxed:
        message = f"policy unsandboxed isn't enabled on {host.name}"
        raise Invalid(message)


def _with_aliases(os: Os, path: str) -> tuple[str, ...]:
    """PATH, and on macOS its `/private` twin when it's under `/tmp`."""
    if os is Os.MACOS and (path == _TMP or path.startswith(f"{_TMP}/")):
        return (path, f"/private{path}")
    return (path,)


def denied_paths(settings: Settings, host: Host, home: str) -> tuple[str, ...]:
    """The paths no harness may read, whatever the OS, profile and harness.

    That's the Connect token store, the session socket directory and the
    config's `denied_home_paths`, each with its `/private` twin on macOS.
    """
    paths: list[str] = []
    if host.os is Os.LINUX:
        # The directory, so a sibling file or a renamed token stays hidden too.
        paths.append(resolve(home, str(secrets.LINUX_TOKEN_FILE.parent)))
    else:
        keychain = secrets.MACOS_KEYCHAIN_FILE
        paths.extend(
            resolve(home, str(p)) for p in (keychain, keychain.with_suffix(".keychain"))
        )
    paths.append(resolve(home, settings.sockets_dir))
    paths.extend(resolve(home, p) for p in settings.sandbox.denied_home_paths)
    return tuple(
        dict.fromkeys(alias for path in paths for alias in _with_aliases(host.os, path))
    )


def environment(
    settings: Settings, host: Host, harness: Harness, paths: RunPaths, account: str
) -> dict[str, str]:
    """The child's whole environment: an allowlist, with no parent values.

    The two config-directory variables name directories under the private
    TMPDIR that run-host creates and fills. They hold no credential.
    """
    search = settings.sandbox.path
    entries = search.linux if host.os is Os.LINUX else search.macos
    env = {
        "PATH": ":".join(resolve(paths.home, e) for e in entries),
        "HOME": paths.home,
        "USER": account,
        "LOGNAME": account,
        "SHELL": host.shell,
        "LANG": LINUX_LANG if host.os is Os.LINUX else MACOS_LANG,
        "TMPDIR": paths.tmpdir,
        "TMPPREFIX": resolve(paths.tmpdir, "zsh"),
        "GH_CONFIG_DIR": resolve(paths.tmpdir, GH_CONFIG_SUBDIR),
        "DOCKER_CONFIG": resolve(paths.tmpdir, DOCKER_CONFIG_SUBDIR),
    }
    if harness is Harness.CODEX:
        env["CODEX_HOME"] = paths.codex_home
    return env


def _write_roots(
    settings: Settings, profile: Profile, paths: RunPaths
) -> tuple[str, ...]:
    if profile is Profile.REVIEW:
        return tuple(dict.fromkeys((paths.tmpdir, paths.audit_dir)))
    roots = (resolve(paths.home, r) for r in settings.sandbox.write_roots)
    common = () if paths.git_common_dir is None else (paths.git_common_dir,)
    return tuple(dict.fromkeys((paths.worktree, *common, paths.tmpdir, *roots)))


def _codex(
    settings: Settings,
    profile: Profile,
    writes: tuple[str, ...],
    deny: tuple[str, ...],
    *,
    full: bool,
) -> CodexPolicy:
    codex = settings.codex
    search = (
        codex.web_search_build if profile is Profile.BUILD else codex.web_search_review
    )
    if full:
        return CodexPolicy(CodexMode.FULL_ACCESS, (), (), deny, APPROVAL_POLICY, search)
    return CodexPolicy(
        CodexMode.PROFILE, (READ_ROOT,), writes, deny, APPROVAL_POLICY, search
    )


def _claude(
    profile: Profile,
    paths: RunPaths,
    writes: tuple[str, ...],
    deny: tuple[str, ...],
    env: dict[str, str],
) -> ClaudePolicy:
    review = profile is Profile.REVIEW
    # An absolute path in a Claude rule starts with a second slash.
    denied_tools = [f"{tool}(/{p}/**)" for p in deny for tool in ("Read", "Edit")]
    deny_write = [paths.worktree] if review else []
    if review and paths.git_common_dir is not None:
        deny_write.append(paths.git_common_dir)
    sandbox: dict[str, object] = {
        "enabled": True,
        "failIfUnavailable": True,
        "autoAllowBashIfSandboxed": True,
        "allowUnsandboxedCommands": False,
        "enableWeakerNetworkIsolation": True,
        "filesystem": {
            "allowWrite": list(writes),
            "denyRead": list(deny),
            "denyWrite": [*deny_write, *deny],
        },
    }
    if review:
        sandbox["network"] = {
            "strictAllowlist": True,
            "allowedDomains": [
                "github.com",
                "api.github.com",
                "*.githubusercontent.com",
            ],
        }
    return ClaudePolicy(
        {
            "env": {
                k: env[k]
                for k in ("TMPDIR", "TMPPREFIX", "GH_CONFIG_DIR", "DOCKER_CONFIG")
            },
            "permissions": {
                "allow": [] if review else [f"Edit(/{p}/**)" for p in writes],
                "deny": [*(_CLAUDE_FILE_TOOLS if review else ()), *denied_tools],
            },
            "sandbox": sandbox,
        }
    )


def _overlaps(first: str, second: str) -> bool:
    one, two = PurePosixPath(first), PurePosixPath(second)
    return one == two or one in two.parents or two in one.parents


def _refuse_overlaps(paths: tuple[str, ...], deny: tuple[str, ...], kind: str) -> None:
    for path in paths:
        for denied in deny:
            if _overlaps(path, denied):
                message = f"policy {kind} {path} overlaps the denied path {denied}"
                raise Invalid(message)


def _agy_write_roots(
    settings: Settings, profile: Profile, paths: RunPaths
) -> tuple[str, ...]:
    if profile is Profile.REVIEW:
        # Reviews only produce audit output, so they need no TMPDIR write grant.
        return (paths.audit_dir,)
    return (
        paths.worktree,
        paths.git_common_dir
        if paths.git_common_dir is not None
        else resolve(paths.worktree, ".git"),
        paths.tmpdir,
        *(resolve(paths.home, p) for p in settings.sandbox.agy.build_write),
    )


def _agy(
    settings: Settings,
    profile: Profile,
    paths: RunPaths,
    env: dict[str, str],
    deny: tuple[str, ...],
) -> AgyPolicy:
    agy = settings.sandbox.agy
    home = paths.home
    if profile is Profile.REVIEW:
        reads = [resolve(home, p) for p in agy.review_read]
    else:
        reads = [resolve(home, p) for p in agy.build_read]
    writes = _agy_write_roots(settings, profile, paths)
    reads += [env["GH_CONFIG_DIR"], env["DOCKER_CONFIG"]]
    # agy has no deny entry, so a grant that reaches a denied path is the leak.
    _refuse_overlaps(tuple(reads), deny, "agy grant")
    grants = [
        *(f"read_file({p})" for p in reads),
        *(f"write_file({p})" for p in writes),
    ]
    return AgyPolicy(tuple(grants))


def build(  # noqa: PLR0913 -- one value per fact the policy depends on
    settings: Settings,
    host: Host,
    *,
    harness: Harness,
    profile: Profile,
    account: str,
    unsandboxed: bool,
    paths: RunPaths,
) -> Policy:
    """The policy of a run of HARNESS with PROFILE on HOST, as ACCOUNT.

    UNSANDBOXED selects Codex's full-access mode, for the one case that
    `decisions/request.py` allows. A build run writes the worktree, the private
    TMPDIR and the config's `write_roots`. A review run reads the worktree and
    writes the private TMPDIR and the audit directory.

    Raises:
        Invalid: ACCOUNT isn't one of the host's accounts, UNSANDBOXED applies
            to anything but a Codex build run on a host that allows it, or a
            write path or an agy read grant reaches a denied path.
    """
    _check(host, harness, profile, account=account, full=unsandboxed)
    deny = denied_paths(settings, host, paths.home)
    env = environment(settings, host, harness, paths, account)
    writes = _write_roots(settings, profile, paths)
    if host.os is Os.MACOS and harness in (Harness.CODEX, Harness.CLAUDE):
        writes = tuple(dict.fromkeys((*writes, _DEV_FD)))
    agy_writes = (
        _agy_write_roots(settings, profile, paths) if harness is Harness.AGY else ()
    )
    _refuse_overlaps((*writes, *agy_writes), deny, "write path")
    if harness is Harness.CODEX:
        detail: CodexPolicy | ClaudePolicy | AgyPolicy = _codex(
            settings, profile, writes, deny, full=unsandboxed
        )
    elif harness is Harness.CLAUDE:
        detail = _claude(profile, paths, writes, deny, env)
    else:
        detail = _agy(settings, profile, paths, env, deny)
    return Policy(harness, profile, env, deny, detail)

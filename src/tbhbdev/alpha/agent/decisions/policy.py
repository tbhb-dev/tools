"""The sandbox policy of a run: what its harness may touch, and its environment.

`build` is a pure function from the settings, the host, the run's choices and
the run's paths to a frozen `Policy`. It reads nothing, checks no path on disk
and never receives a parent environment. Every path in the policy is absolute,
and a config path that isn't absolute resolves against the run account's home.
There are no default lists here: write roots, denied paths and `PATH` come from
`[agent.sandbox]`.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import PurePosixPath

from tbhbdev import secrets
from tbhbdev.alpha.agent import host_tools
from tbhbdev.alpha.agent.model import (
    AgyPolicy,
    ClaudePolicy,
    CodexMode,
    CodexPolicy,
    Harness,
    Host,
    Invalid,
    NetworkProxy,
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
_REVIEW_DOMAINS = ("github.com", "api.github.com", "*.githubusercontent.com")
# The handoff directories, under the run's private TMPDIR. run-host creates
# each one, and the child gets its path in the variable of the same tool.
GH_CONFIG_SUBDIR = "gh"
DOCKER_CONFIG_SUBDIR = "docker"
# macOS's `/tmp` is a link to `/private/tmp`, so a denied path under it needs both.
_TMP = "/tmp"  # noqa: S108 # nosec B108 -- an alias, not a temp file
# Claude's file tools, which a review run doesn't get.
_CLAUDE_FILE_TOOLS = ("Edit", "Write", "NotebookEdit")
GIT_CONFIG_PATHS = (
    ".git",
    ".git/config",
    ".git/config.worktree",
    ".git/commondir",
    ".git/hooks",
    ".git/info",
    ".git/modules",
)


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


def denied_paths(
    settings: Settings, host: Host, home: str, uid: int
) -> tuple[str, ...]:
    """The paths no harness may read, whatever the OS, profile and harness.

    That's the Connect token store, the session socket directory and the
    config's `denied_home_paths`, each with its `/private` twin on macOS.

    A path under another denied path is dropped, because the sandbox can't
    mount a denied child under a denied parent. The test compares path strings,
    not resolved targets, so a symlink inside a denied directory that points
    elsewhere isn't denied by this function.
    """
    paths: list[str] = []
    if host.os is Os.LINUX:
        # The directory, so a sibling file or a renamed token stays hidden too.
        paths.append(resolve(home, str(secrets.LINUX_TOKEN_FILE.parent)))
        paths.extend(
            ("/run/WSL", "/var/run/docker.sock", "/run/docker.sock", f"/run/user/{uid}")
        )
    else:
        keychain = secrets.MACOS_KEYCHAIN_FILE
        paths.extend(
            resolve(home, str(p)) for p in (keychain, keychain.with_suffix(".keychain"))
        )
    paths.append(resolve(home, settings.sockets_dir))
    paths.extend(resolve(home, p) for p in settings.sandbox.denied_home_paths)
    paths.extend(c.signing for c in settings.credentials if c.signing.startswith("/"))
    unique = tuple(
        dict.fromkeys(alias for path in paths for alias in _with_aliases(host.os, path))
    )
    # A child under a denied parent is already hidden. Masking it as well makes
    # bwrap mkdir its mount point inside the parent's read-only mask and fail.
    return tuple(
        path
        for path in unique
        if not any(str(parent) in unique for parent in PurePosixPath(path).parents)
    )


def environment(
    settings: Settings, host: Host, harness: Harness, paths: RunPaths, account: str
) -> dict[str, str]:
    """The child's whole environment: an allowlist, with no parent values.

    The two config-directory variables name directories under the private
    TMPDIR that run-host creates and fills. They hold no credential.
    """
    env = {
        "PATH": ":".join(host_tools.search_path(settings, host.os)),
        "HOME": paths.home,
        "USER": account,
        "LOGNAME": account,
        "SHELL": f"/bin/{host.shell}",
        "LANG": LINUX_LANG if host.os is Os.LINUX else MACOS_LANG,
        "TMPDIR": paths.tmpdir,
        "TMPPREFIX": resolve(paths.tmpdir, "zsh"),
        "GH_CONFIG_DIR": resolve(paths.tmpdir, GH_CONFIG_SUBDIR),
        "DOCKER_CONFIG": resolve(paths.tmpdir, DOCKER_CONFIG_SUBDIR),
        "GIT_NO_LAZY_FETCH": "1",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_COUNT": "8",
    }
    overrides = (
        ("core.fsmonitor", "false"),
        ("core.hooksPath", "/dev/null"),
        ("diff.ignoreSubmodules", "all"),
        ("status.submoduleSummary", "false"),
        ("protocol.ext.allow", "never"),
        ("protocol.file.allow", "never"),
        ("diff.external", ""),
        ("safe.bareRepository", "explicit"),
    )
    for index, (key, value) in enumerate(overrides):
        env[f"GIT_CONFIG_KEY_{index}"] = key
        env[f"GIT_CONFIG_VALUE_{index}"] = value
    if harness is Harness.CODEX:
        env["CODEX_HOME"] = paths.codex_home
    if paths.signing_socket is not None:
        env["SSH_AUTH_SOCK"] = paths.signing_socket
    return env


def review_environment(profile: Profile, paths: RunPaths) -> dict[str, str]:
    if profile is Profile.REVIEW:
        return {"GH_CONFIG_DIR": resolve(paths.tmpdir, GH_CONFIG_SUBDIR)}
    return {}


def _write_roots(
    settings: Settings, profile: Profile, paths: RunPaths
) -> tuple[str, ...]:
    if profile is Profile.REVIEW:
        return tuple(dict.fromkeys((paths.tmpdir, paths.audit_dir)))
    roots = (resolve(paths.home, r) for r in settings.sandbox.write_roots)
    return tuple(dict.fromkeys((paths.worktree, paths.tmpdir, *roots)))


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
    proxy = network_proxy(settings)
    if full:
        return CodexPolicy(
            CodexMode.FULL_ACCESS, (), (), deny, APPROVAL_POLICY, search, proxy
        )
    return CodexPolicy(
        CodexMode.PROFILE,
        (READ_ROOT,),
        writes,
        deny,
        APPROVAL_POLICY,
        search,
        proxy,
    )


def protected_git(paths: RunPaths) -> tuple[str, ...]:
    return tuple(resolve(paths.worktree, entry) for entry in GIT_CONFIG_PATHS)


def _claude_write_denies(profile: Profile, paths: RunPaths) -> tuple[str, ...]:
    if profile is Profile.BUILD:
        return protected_git(paths)
    return (paths.worktree, *protected_git(paths))


def _claude(
    profile: Profile,
    paths: RunPaths,
    access: tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]],
    env: dict[str, str],
    domains: tuple[str, ...],
) -> ClaudePolicy:
    writes, deny, protected = access
    review = profile is Profile.REVIEW
    # An absolute path in a Claude rule starts with a second slash.
    denied_tools = [f"{tool}(/{p}/**)" for p in deny for tool in ("Read", "Edit")]
    deny_write = (*_claude_write_denies(profile, paths), *protected)
    denied_tools.extend(
        f"{tool}(/{p}{suffix})"
        for p in deny_write
        for suffix in ("", "/**")
        for tool in ("Edit", "Write")
    )
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
    network: dict[str, object] = {
        "allowedDomains": list(domains),
        "allowAllUnixSockets": False,
        "allowLocalBinding": False,
        "strictAllowlist": True,
    }
    if review:
        network.update(
            {
                "allowedDomains": list(_REVIEW_DOMAINS),
            }
        )
    if paths.signing_socket is not None:
        network["allowUnixSockets"] = [paths.signing_socket]
    sandbox["network"] = network
    return ClaudePolicy(
        {
            "disableAllHooks": True,
            "env": {
                k: env[k]
                for k in (
                    "TMPDIR",
                    "PATH",
                    "TMPPREFIX",
                    "GH_CONFIG_DIR",
                    "DOCKER_CONFIG",
                    "GIT_NO_LAZY_FETCH",
                    "SSH_AUTH_SOCK",
                )
                if k in env
            },
            "permissions": {
                "allow": [] if review else [f"Edit(/{p}/**)" for p in writes],
                "deny": [
                    *(_CLAUDE_FILE_TOOLS if review else ()),
                    *denied_tools,
                    "WebFetch",
                    "WebSearch",
                ],
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


def protect_host(
    settings: Settings, paths: RunPaths, protected: tuple[str, ...]
) -> None:
    """Refuse host trust paths on either side of a writable path."""
    writes = (
        paths.worktree,
        paths.tmpdir,
        paths.audit_dir,
        _DEV_FD,
        *(resolve(paths.home, p) for p in settings.sandbox.write_roots),
        *(resolve(paths.home, p) for p in settings.sandbox.agy.build_write),
    )
    protect_paths(writes, protected)
    protect_paths(writes[1:], (resolve(paths.worktree, ".git/hooks"),))


def protect_paths(writes: tuple[str, ...], protected: tuple[str, ...]) -> None:
    """Refuse either an ancestor or a descendant of a host trust path."""
    for path in protected:
        for write in writes:
            if _overlaps(path, write):
                message = (
                    f"protected host path {path} overlaps write root {write}; "
                    "narrow that write root"
                )
                raise Invalid(message)


def network_proxy(settings: Settings) -> NetworkProxy | None:
    if settings.sandbox.codex_network_proxy_opt_out is not None:
        return None
    return NetworkProxy(
        enabled=True,
        domains=dict.fromkeys(settings.sandbox.network_domains, "allow"),
        mode="limited",
        unix_sockets={},
        dangerously_allow_all_unix_sockets=False,
        allow_local_binding=False,
    )


def _agy_write_roots(
    settings: Settings, profile: Profile, paths: RunPaths
) -> tuple[str, ...]:
    if profile is Profile.REVIEW:
        # Reviews only produce audit output, so they need no TMPDIR write grant.
        return (paths.audit_dir,)
    return (
        paths.worktree,
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
    reads += [env[k] for k in ("GH_CONFIG_DIR", "DOCKER_CONFIG") if k in env]
    if paths.signing_socket is not None:
        reads.append(paths.signing_socket)
    # agy has no deny entry, so a grant that reaches a denied path is the leak.
    _refuse_overlaps(tuple(reads), deny, "agy grant")
    grants = [
        *(f"read_file({p})" for p in reads),
        *(f"write_file({p})" for p in writes),
    ]
    return AgyPolicy(tuple(grants))


def tool_path(settings: Settings, host: Host, home: str) -> str:
    search = settings.sandbox.path
    entries = search.linux if host.os is Os.LINUX else search.macos
    return ":".join(resolve(home, e) for e in entries)


def build(  # noqa: PLR0913 -- one value per fact the policy depends on
    settings: Settings,
    host: Host,
    *,
    harness: Harness,
    profile: Profile,
    account: str,
    unsandboxed: bool,
    paths: RunPaths,
    protected: tuple[str, ...] = (),
    denied: tuple[str, ...] | None = None,
) -> Policy:
    """The policy of a run of HARNESS with PROFILE on HOST, as ACCOUNT.

    UNSANDBOXED selects Codex's full-access mode, for the one case that
    `decisions/request.py` allows. A build run writes the worktree, the private
    TMPDIR and the config's `write_roots`. A review run reads the worktree and
    writes the private TMPDIR and the audit directory.

    Raises:
        Invalid: ACCOUNT isn't one of the host's accounts, UNSANDBOXED applies
            to anything but a Codex build run on a host that allows it, or a
            write path, signing socket or an agy read grant reaches a denied path.
    """
    _check(host, harness, profile, account=account, full=unsandboxed)
    if harness is Harness.AGY and settings.sandbox.agy_unsandboxed_opt_out is None:
        message = (
            "agy has no OS sandbox; configure agy_unsandboxed_opt_out "
            "with a named reason"
        )
        raise Invalid(message)
    if (
        harness is Harness.CODEX
        and unsandboxed
        and settings.sandbox.codex_unsandboxed_opt_out is None
    ):
        message = (
            "codex has no OS sandbox; configure codex_unsandboxed_opt_out "
            "with a named reason"
        )
        raise Invalid(message)
    protect_host(settings, paths, protected)
    deny = (
        denied_paths(settings, host, paths.home, paths.uid)
        if denied is None
        else denied
    )
    if paths.signing_socket is not None:
        _refuse_overlaps((paths.signing_socket,), deny, "signing socket")
    env = environment(settings, host, harness, paths, account) | review_environment(
        profile, paths
    )
    tools_path = tool_path(settings, host, paths.home)
    writes = _write_roots(settings, profile, paths)
    if host.os is Os.MACOS and harness in (Harness.CODEX, Harness.CLAUDE):
        writes = tuple(dict.fromkeys((*writes, _DEV_FD)))
    agy_writes = (
        _agy_write_roots(settings, profile, paths) if harness is Harness.AGY else ()
    )
    _refuse_overlaps((*writes, *agy_writes), deny, "write path")
    detail: CodexPolicy | ClaudePolicy | AgyPolicy
    if harness is Harness.CODEX:
        codex_detail = _codex(settings, profile, writes, deny, full=unsandboxed)
        if profile is Profile.BUILD and not unsandboxed:
            codex_detail = replace(
                codex_detail,
                read_roots=(
                    *codex_detail.read_roots,
                    *protected_git(paths)[1:],
                ),
                write_roots=(
                    paths.worktree,
                    resolve(paths.worktree, ".git"),
                    *codex_detail.write_roots[1:],
                ),
            )
        detail = codex_detail
    elif harness is Harness.CLAUDE:
        detail = _claude(
            profile,
            paths,
            (writes, deny, protected),
            env | {"PATH": tools_path},
            settings.sandbox.network_domains,
        )
    else:
        detail = _agy(settings, profile, paths, env, deny)
    return Policy(harness, profile, env, deny, detail, tools_path)

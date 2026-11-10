"""The sandbox policy is a table over literal values, with no file and no process."""

# ruff: noqa: S108 -- the paths are test data under /tmp that no test creates

from __future__ import annotations

import dataclasses
import itertools
from pathlib import PurePosixPath
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from typing import Literal

import pytest
from agent_example import (
    DENIED,
    LINUX,
    LINUX_SOCKET_DENIES,
    MACOS,
    MARKER,
    SANDBOX,
    TOKEN_DIR,
    TOKEN_TOP,
    paths,
    settings,
)

from tbhbdev import secrets
from tbhbdev.alpha.agent import model
from tbhbdev.alpha.agent.decisions import policy
from tbhbdev.alpha.agent.model import (
    Harness,
    Invalid,
    Os,
    Profile,
    WebSearch,
)

HOSTS = {Os.LINUX: LINUX, Os.MACOS: MACOS}
ACCOUNTS = ["agent", "ops"]
COMBINATIONS = list(
    itertools.product(Os, Profile, Harness, ACCOUNTS),
)
IDS = [f"{o.value}-{p.value}-{h.value}-{a}" for o, p, h, a in COMBINATIONS]
MACOS_TOKEN_PATHS = (
    "Library/Keychains/tbhbdev.keychain-db",
    "Library/Keychains/tbhbdev.keychain",
)
PATH = {
    Os.LINUX: "{home}/.local/share/mise/shims:/usr/local/bin:/usr/bin",
    Os.MACOS: "{home}/.local/share/mise/shims:/opt/example/bin:/usr/bin",
}


def made(  # noqa: PLR0913 -- one value per fact the policy depends on
    os: Os,
    profile: Profile,
    harness: Harness,
    account: str,
    *,
    unsandboxed: bool = False,
    config: model.Settings | None = None,
) -> model.Policy:
    return policy.build(
        config or settings(),
        HOSTS[os],
        harness=harness,
        profile=profile,
        account=account,
        unsandboxed=unsandboxed,
        paths=paths(account),
    )


def claude_settings_of(result: model.Policy) -> dict[str, object]:
    detail = result.detail
    assert isinstance(detail, model.ClaudePolicy)
    return detail.settings


def strings(node: object) -> list[str]:
    assert isinstance(node, list)
    items: list[object] = list(node)
    assert all(isinstance(item, str) for item in items)
    return [str(item) for item in items]


def dig(node: object, *keys: str) -> object:
    for key in keys:
        assert isinstance(node, dict)
        node = node[key]
    return node


def denied(os: Os, home: str) -> tuple[str, ...]:
    """The literal deny list: the token store, the sockets, a denied path."""
    if os is Os.LINUX:
        return (
            f"{home}/{TOKEN_DIR}",
            *LINUX_SOCKET_DENIES,
            "/tmp/example-sockets",
            f"{home}/{DENIED}",
        )
    return (
        *(f"{home}/{p}" for p in MACOS_TOKEN_PATHS),
        "/tmp/example-sockets",
        "/private/tmp/example-sockets",
        f"{home}/{DENIED}",
    )


def env_of(os: Os, harness: Harness, account: str) -> dict[str, str]:
    home = f"/example/home/{account}"
    tmp = f"{home}/.local/state/example-runs/run1.tmp"
    env = {
        "PATH": PATH[os].format(home=home),
        "HOME": home,
        "USER": account,
        "LOGNAME": account,
        "SHELL": "bash" if os is Os.LINUX else "zsh",
        "LANG": "C.UTF-8" if os is Os.LINUX else "en_US.UTF-8",
        "TMPDIR": tmp,
        "TMPPREFIX": f"{tmp}/zsh",
        "GH_CONFIG_DIR": f"{tmp}/gh",
        "DOCKER_CONFIG": f"{tmp}/docker",
    }
    if harness is Harness.CODEX:
        env["CODEX_HOME"] = f"{tmp}/codex"
    return env


@pytest.mark.parametrize(("os", "profile", "harness", "account"), COMBINATIONS, ids=IDS)
def test_environment_is_exactly_the_allowlist(
    os: Os,
    profile: Profile,
    harness: Harness,
    account: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # No parent value reaches the child: the function never receives one.
    for name in (
        "PATH",
        "HOME",
        "TMPDIR",
        "TMPPREFIX",
        "GH_CONFIG_DIR",
        "USER",
        "LOGNAME",
        "SHELL",
        "LANG",
        "TERM",
        "EXAMPLE_PARENT",
    ):
        monkeypatch.setenv(name, "/parent-value")
    result = made(os, profile, harness, account)
    assert result.env == env_of(os, harness, account)
    assert result.env["TMPPREFIX"] == result.env["TMPDIR"] + "/zsh"


@pytest.mark.parametrize(("os", "profile", "harness", "account"), COMBINATIONS, ids=IDS)
def test_every_output_denies_the_same_paths(
    os: Os, profile: Profile, harness: Harness, account: str
) -> None:
    home = f"/example/home/{account}"
    result = made(os, profile, harness, account)
    assert result.deny == denied(os, home)
    assert (result.harness, result.profile) == (harness, profile)
    if isinstance(result.detail, model.CodexPolicy):
        assert result.detail.deny == denied(os, home)
    elif isinstance(result.detail, model.ClaudePolicy):
        claude = claude_settings_of(result)
        fs = ("sandbox", "filesystem")
        assert dig(claude, *fs, "denyRead") == list(denied(os, home))
        assert dig(claude, *fs, "denyWrite") == [
            *(() if profile is Profile.BUILD else (paths(account).worktree,)),
            *denied(os, home),
        ]
        rules = strings(dig(claude, "permissions", "deny"))
        reads = [r for r in rules if r.startswith("Read(")]
        assert reads == [f"Read(/{p}/**)" for p in denied(os, home)]
        edits = [r for r in rules if r.startswith("Edit(")]
        assert edits == [f"Edit(/{p}/**)" for p in denied(os, home)]
    else:
        assert not any(d in g for d in denied(os, home) for g in result.detail.grants)


def codex_expected(
    os: Os, profile: Profile, account: str, search: WebSearch
) -> model.CodexPolicy:
    home = f"/example/home/{account}"
    tmp = f"{home}/.local/state/example-runs/run1.tmp"
    if profile is Profile.BUILD:
        writes = (
            f"{home}/Code/example.com/example-org/example-repo",
            tmp,
            f"{home}/.cache",
            f"{home}/.local/share/mise",
            f"{home}/.local/state",
        )
    else:
        writes = (tmp, f"{home}/.local/state/example-audit")
    if os is Os.MACOS:
        writes = (*writes, "/dev/fd")
    return model.CodexPolicy(
        mode=model.CodexMode.PROFILE,
        read_roots=(":root",),
        write_roots=writes,
        deny=denied(os, home),
        approval_policy="never",
        web_search=search,
        network_proxy=policy.network_proxy(settings()),
    )


@pytest.mark.parametrize("account", ACCOUNTS)
@pytest.mark.parametrize("profile", Profile)
@pytest.mark.parametrize("os", Os)
def test_codex_profile(os: Os, profile: Profile, account: str) -> None:
    result = made(os, profile, Harness.CODEX, account)
    assert result.detail == codex_expected(os, profile, account, WebSearch.LIVE)


@pytest.mark.parametrize(
    ("profile", "build", "review"),
    [
        (Profile.BUILD, WebSearch.CACHED, WebSearch.DISABLED),
        (Profile.REVIEW, WebSearch.CACHED, WebSearch.DISABLED),
        (Profile.BUILD, WebSearch.DISABLED, WebSearch.CACHED),
        (Profile.REVIEW, WebSearch.DISABLED, WebSearch.CACHED),
        (Profile.BUILD, WebSearch.LIVE, WebSearch.DISABLED),
        (Profile.REVIEW, WebSearch.DISABLED, WebSearch.LIVE),
    ],
)
def test_web_search_follows_the_profile(
    profile: Profile, build: WebSearch, review: WebSearch
) -> None:
    config = settings(codex=model.Codex(build, review))
    result = made(Os.LINUX, profile, Harness.CODEX, "agent", config=config)
    expected = build if profile is Profile.BUILD else review
    assert isinstance(result.detail, model.CodexPolicy)
    assert result.detail.web_search is expected


def test_a_review_makes_the_worktree_read_only() -> None:
    result = made(Os.LINUX, Profile.REVIEW, Harness.CODEX, "agent")
    assert isinstance(result.detail, model.CodexPolicy)
    worktree = paths("agent").worktree
    assert worktree not in result.detail.write_roots
    assert result.detail.read_roots == (":root",)


@pytest.mark.parametrize("os", Os)
def test_unsandboxed_selects_full_access_for_a_codex_build(os: Os) -> None:
    host = dataclasses.replace(HOSTS[os], unsandboxed=True)
    config = dataclasses.replace(settings(), hosts=(host,))
    result = policy.build(
        config,
        host,
        harness=Harness.CODEX,
        profile=Profile.BUILD,
        account="ops",
        unsandboxed=True,
        paths=paths("ops"),
    )
    assert result.detail == model.CodexPolicy(
        mode=model.CodexMode.FULL_ACCESS,
        read_roots=(),
        write_roots=(),
        deny=denied(os, "/example/home/ops"),
        approval_policy="never",
        web_search=WebSearch.LIVE,
        network_proxy=policy.network_proxy(settings()),
    )
    assert result.env == env_of(os, Harness.CODEX, "ops")
    assert result.deny == denied(os, "/example/home/ops")


def test_a_host_that_allows_it_still_sandboxes_unless_asked() -> None:
    result = made(Os.LINUX, Profile.BUILD, Harness.CODEX, "agent")
    assert isinstance(result.detail, model.CodexPolicy)
    assert result.detail.mode is model.CodexMode.PROFILE


@pytest.mark.parametrize(
    ("host", "harness", "profile", "account", "why"),
    [
        (LINUX, Harness.CLAUDE, Profile.BUILD, "agent", "applies only to a codex run"),
        (LINUX, Harness.AGY, Profile.BUILD, "agent", "applies only to a codex run"),
        (LINUX, Harness.CODEX, Profile.REVIEW, "agent", "applies only to a build run"),
        (
            dataclasses.replace(LINUX, unsandboxed=False),
            Harness.CODEX,
            Profile.BUILD,
            "agent",
            "isn't enabled on example-linux",
        ),
    ],
)
def test_unsandboxed_is_refused_outside_its_one_case(
    host: model.Host, harness: Harness, profile: Profile, account: str, why: str
) -> None:
    with pytest.raises(Invalid) as caught:
        policy.build(
            settings(),
            host,
            harness=harness,
            profile=profile,
            account=account,
            unsandboxed=True,
            paths=paths(account),
        )
    assert str(caught.value) == f"policy unsandboxed {why}"


@pytest.mark.parametrize("unsandboxed", [False, True])
def test_an_account_the_host_lacks_is_refused(unsandboxed: bool) -> None:
    with pytest.raises(Invalid) as caught:
        policy.build(
            settings(),
            dataclasses.replace(LINUX, accounts=("agent",)),
            harness=Harness.CODEX,
            profile=Profile.BUILD,
            account="ops",
            unsandboxed=unsandboxed,
            paths=paths("ops"),
        )
    assert (
        str(caught.value)
        == "policy account 'ops' isn't one of the accounts of example-linux"
    )


def claude_expected(os: Os, profile: Profile, account: str) -> dict[str, object]:
    home = f"/example/home/{account}"
    tmp = f"{home}/.local/state/example-runs/run1.tmp"
    worktree = f"{home}/Code/example.com/example-org/example-repo"
    deny = list(denied(os, home))
    denied_tools = [f"{tool}(/{p}/**)" for p in deny for tool in ("Read", "Edit")]
    if profile is Profile.BUILD:
        writes = [
            worktree,
            tmp,
            f"{home}/.cache",
            f"{home}/.local/share/mise",
            f"{home}/.local/state",
        ]
        if os is Os.MACOS:
            writes.append("/dev/fd")
        allow = [f"Edit(/{p}/**)" for p in writes]
        rules = denied_tools
        deny_write = deny
    else:
        writes = [tmp, f"{home}/.local/state/example-audit"]
        if os is Os.MACOS:
            writes.append("/dev/fd")
        allow: list[str] = []
        rules = ["Edit", "Write", "NotebookEdit", *denied_tools]
        deny_write = [worktree, *deny]
    return {
        "env": {
            "TMPDIR": tmp,
            "TMPPREFIX": f"{tmp}/zsh",
            "GH_CONFIG_DIR": f"{tmp}/gh",
            "DOCKER_CONFIG": f"{tmp}/docker",
        },
        "permissions": {"allow": allow, "deny": rules},
        "sandbox": {
            "allowUnsandboxedCommands": False,
            "autoAllowBashIfSandboxed": True,
            "enabled": True,
            "failIfUnavailable": True,
            "enableWeakerNetworkIsolation": True,
            **(
                {
                    "network": {
                        "strictAllowlist": True,
                        "allowedDomains": [
                            "github.com",
                            "api.github.com",
                            "*.githubusercontent.com",
                        ],
                    },
                }
                if profile is Profile.REVIEW
                else {"network": {"allowedDomains": []}}
            ),
            "filesystem": {
                "allowWrite": writes,
                "denyRead": deny,
                "denyWrite": deny_write,
            },
        },
    }


@pytest.mark.parametrize("account", ACCOUNTS)
@pytest.mark.parametrize("profile", Profile)
@pytest.mark.parametrize("os", Os)
def test_claude_settings(os: Os, profile: Profile, account: str) -> None:
    result = made(os, profile, Harness.CLAUDE, account)
    assert result.detail == model.ClaudePolicy(claude_expected(os, profile, account))


def agy_expected(profile: Profile, account: str) -> tuple[str, ...]:
    home = f"/example/home/{account}"
    tmp = f"{home}/.local/state/example-runs/run1.tmp"
    handoff = (f"read_file({tmp}/gh)", f"read_file({tmp}/docker)")
    if profile is Profile.REVIEW:
        return (
            f"read_file({home}/.local/share/example-gh)",
            f"read_file({home}/.local/share/example-mise)",
            *handoff,
            f"write_file({home}/.local/state/example-audit)",
        )
    worktree = f"{home}/Code/example.com/example-org/example-repo"
    return (
        f"read_file({home}/.local/share/example-tools)",
        *handoff,
        f"write_file({worktree})",
        f"write_file({worktree}/.git)",
        f"write_file({tmp})",
        f"write_file({home}/.local/state/mise)",
        f"write_file({home}/.local/share/mise)",
    )


@pytest.mark.parametrize("account", ACCOUNTS)
@pytest.mark.parametrize("profile", Profile)
@pytest.mark.parametrize("os", Os)
def test_agy_grants(os: Os, profile: Profile, account: str) -> None:
    result = made(os, profile, Harness.AGY, account)
    assert result.detail == model.AgyPolicy(agy_expected(profile, account))


@pytest.mark.parametrize("os", Os)
@pytest.mark.parametrize("profile", Profile)
def test_write_roots_keep_the_first_occurrence(os: Os, profile: Profile) -> None:
    run = paths("agent")
    sandbox = dataclasses.replace(
        SANDBOX,
        write_roots=(
            ".local/state/example-runs/run1.tmp",
            ".cache",
            "Code/example.com/example-org/example-repo",
            ".cache",
        ),
    )
    result = policy.build(
        settings(sandbox=sandbox),
        HOSTS[os],
        harness=Harness.CODEX,
        profile=profile,
        account="agent",
        unsandboxed=False,
        paths=dataclasses.replace(run, audit_dir=run.tmpdir),
    )
    assert isinstance(result.detail, model.CodexPolicy)
    expected = (
        (run.worktree, run.tmpdir, f"{run.home}/.cache")
        if profile is Profile.BUILD
        else (run.tmpdir,)
    )
    assert result.detail.write_roots == (
        *expected,
        *(("/dev/fd",) if os is Os.MACOS else ()),
    )


@pytest.mark.parametrize("os", Os)
@pytest.mark.parametrize("profile", Profile)
def test_denied_paths_keep_the_first_occurrence(os: Os, profile: Profile) -> None:
    sandbox = dataclasses.replace(
        SANDBOX, denied_home_paths=(DENIED, TOKEN_DIR, DENIED)
    )
    config = dataclasses.replace(
        settings(sandbox=sandbox), sockets_dir=f"/example/home/agent/{DENIED}"
    )
    result = made(os, profile, Harness.CODEX, "agent", config=config)
    home = "/example/home/agent"
    expected = (
        (
            f"{home}/{TOKEN_DIR}",
            *LINUX_SOCKET_DENIES,
            f"{home}/{DENIED}",
        )
        if os is Os.LINUX
        else (
            *(f"{home}/{p}" for p in MACOS_TOKEN_PATHS),
            f"{home}/{DENIED}",
            f"{home}/{TOKEN_DIR}",
        )
    )
    assert result.deny == expected
    assert isinstance(result.detail, model.CodexPolicy)
    assert result.detail.deny == expected


@pytest.mark.parametrize(
    ("os", "grant", "denied_path"),
    [
        (Os.LINUX, TOKEN_TOP, "{home}/" + TOKEN_DIR),
        (Os.LINUX, TOKEN_DIR, "{home}/" + TOKEN_DIR),
        (Os.LINUX, TOKEN_DIR + "/inner", "{home}/" + TOKEN_DIR),
        (Os.LINUX, ".", "{home}/" + TOKEN_DIR),
        (Os.MACOS, ".", "{home}/Library/Keychains/tbhbdev.keychain-db"),
        (Os.MACOS, DENIED, "{home}/" + DENIED),
        (Os.MACOS, DENIED + "/inner", "{home}/" + DENIED),
        (Os.MACOS, "/private/tmp", "/private/tmp/example-sockets"),
        (Os.MACOS, "/tmp/example-sockets/inner", "/tmp/example-sockets"),
    ],
)
@pytest.mark.parametrize("profile", Profile)
def test_an_agy_read_grant_that_reaches_a_denied_path_is_refused(
    os: Os, profile: Profile, grant: str, denied_path: str
) -> None:
    agy = dataclasses.replace(SANDBOX.agy, review_read=(grant,), build_read=(grant,))
    config = settings(sandbox=dataclasses.replace(SANDBOX, agy=agy))
    home = "/example/home/agent"
    shown = (
        grant
        if grant.startswith("/")
        else (home if grant == "." else f"{home}/{grant}")
    )
    with pytest.raises(Invalid) as caught:
        made(os, profile, Harness.AGY, "agent", config=config)
    assert str(caught.value) == (
        f"policy agy grant {shown} overlaps the denied path "
        f"{denied_path.format(home=home)}"
    )


@pytest.mark.parametrize("sibling", [DENIED + "x", DENIED + "-other", TOKEN_DIR + "2"])
def test_a_grant_beside_a_denied_path_is_allowed(sibling: str) -> None:
    agy = model.AgySandbox((sibling,), (sibling,), (sibling,))
    config = settings(sandbox=dataclasses.replace(SANDBOX, agy=agy))
    for profile in Profile:
        result = made(Os.LINUX, profile, Harness.AGY, "agent", config=config)
        assert isinstance(result.detail, model.AgyPolicy)
        assert f"/example/home/agent/{sibling})" in "".join(result.detail.grants)


def test_the_token_store_is_the_directory_secrets_names() -> None:
    assert not secrets.LINUX_TOKEN_FILE.parent.is_absolute()
    result = made(Os.LINUX, Profile.BUILD, Harness.CODEX, "agent")
    assert f"/example/home/agent/{TOKEN_DIR}" in result.deny


@pytest.mark.parametrize("harness", Harness)
@pytest.mark.parametrize("profile", Profile)
@pytest.mark.parametrize("account", ACCOUNTS)
def test_macos_denies_the_keychain_file_and_alias(
    harness: Harness, profile: Profile, account: str
) -> None:
    assert secrets.MACOS_KEYCHAIN_FILE.as_posix() == (
        "Library/Keychains/tbhbdev.keychain-db"
    )
    assert not secrets.MACOS_KEYCHAIN_FILE.is_absolute()
    result = made(Os.MACOS, profile, harness, account)
    assert result.deny[:2] == (
        f"/example/home/{account}/Library/Keychains/tbhbdev.keychain-db",
        f"/example/home/{account}/Library/Keychains/tbhbdev.keychain",
    )


@pytest.mark.parametrize("profile", Profile)
@pytest.mark.parametrize(
    "grant",
    [
        "Library",
        "Library/Keychains/tbhbdev.keychain-db",
        "Library/Keychains/tbhbdev.keychain",
    ],
)
def test_agy_refuses_grants_reaching_the_macos_keychain(
    profile: Profile, grant: str
) -> None:
    agy = model.AgySandbox((grant,), (grant,), (grant,))
    config = settings(sandbox=dataclasses.replace(SANDBOX, agy=agy))
    with pytest.raises(Invalid, match="overlaps the denied path"):
        made(Os.MACOS, profile, Harness.AGY, "agent", config=config)


@pytest.mark.parametrize(
    ("sockets", "os", "expected"),
    [
        ("/tmp", Os.MACOS, ("/tmp", "/private/tmp")),
        ("/tmp/a/b", Os.MACOS, ("/tmp/a/b", "/private/tmp/a/b")),
        ("/tmp/a", Os.LINUX, ("/tmp/a",)),
        ("/tmp-x/a", Os.MACOS, ("/tmp-x/a",)),
        ("/var/tmp/a", Os.MACOS, ("/var/tmp/a",)),
        (".local/run/sockets", Os.MACOS, ("/example/home/agent/.local/run/sockets",)),
        ("/example/sockets", Os.LINUX, ("/example/sockets",)),
    ],
)
def test_the_sockets_directory_is_denied_with_its_aliases(
    sockets: str, os: Os, expected: tuple[str, ...]
) -> None:
    config = dataclasses.replace(settings(), sockets_dir=sockets)
    result = made(os, Profile.REVIEW, Harness.CODEX, "agent", config=config)
    home = "/example/home/agent"
    assert result.deny == (
        *(
            (
                f"{home}/{TOKEN_DIR}",
                "/run/WSL",
                "/var/run/docker.sock",
                "/run/docker.sock",
                "/run/user/0",
            )
            if os is Os.LINUX
            else tuple(f"{home}/{p}" for p in MACOS_TOKEN_PATHS)
        ),
        *expected,
        f"{home}/{DENIED}",
    )


def test_denied_home_paths_each_resolve_against_the_home() -> None:
    sandbox = dataclasses.replace(
        SANDBOX,
        denied_home_paths=(DENIED, ".local/state/x", "/tmp/y"),
        write_roots=(".cache",),
    )
    result = made(
        Os.MACOS, Profile.BUILD, Harness.CODEX, "ops", config=settings(sandbox=sandbox)
    )
    home = "/example/home/ops"
    assert result.deny == (
        *(f"{home}/{p}" for p in MACOS_TOKEN_PATHS),
        "/tmp/example-sockets",
        "/private/tmp/example-sockets",
        f"{home}/{DENIED}",
        f"{home}/.local/state/x",
        "/tmp/y",
        "/private/tmp/y",
    )


@pytest.mark.parametrize(
    ("home", "path", "expected"),
    [
        ("/h", "a/b", "/h/a/b"),
        ("/h", "/abs/a", "/abs/a"),
        ("/h/", "a", "/h/a"),
    ],
)
def test_resolve(home: str, path: str, expected: str) -> None:
    assert policy.resolve(home, path) == expected


def test_write_roots_come_only_from_the_config() -> None:
    sandbox = dataclasses.replace(SANDBOX, write_roots=(".only-this", "/example/abs"))
    result = made(
        Os.LINUX,
        Profile.BUILD,
        Harness.CODEX,
        "agent",
        config=settings(sandbox=sandbox),
    )
    assert isinstance(result.detail, model.CodexPolicy)
    home = "/example/home/agent"
    assert result.detail.write_roots == (
        f"{home}/Code/example.com/example-org/example-repo",
        f"{home}/.local/state/example-runs/run1.tmp",
        f"{home}/.only-this",
        "/example/abs",
    )


def test_a_review_ignores_the_build_write_roots() -> None:
    result = made(Os.MACOS, Profile.REVIEW, Harness.CLAUDE, "agent")
    assert dig(claude_settings_of(result), "sandbox", "filesystem", "allowWrite") == [
        "/example/home/agent/.local/state/example-runs/run1.tmp",
        "/example/home/agent/.local/state/example-audit",
        "/dev/fd",
    ]


def test_no_output_holds_a_secret_reference() -> None:
    config = settings(reference=f"{MARKER}/{MARKER}/{MARKER}")
    for os, profile, harness in itertools.product(Os, Profile, Harness):
        result = made(os, profile, harness, "agent", config=config)
        assert MARKER not in repr(result)


@pytest.mark.parametrize("os", Os)
@pytest.mark.parametrize("harness", Harness)
@pytest.mark.parametrize(
    "common",
    [
        None,
        "/example/clone/.git",
        "/example/home/agent/.local/state/example-runs/run1.tmp",
    ],
)
def test_build_grants_the_git_common_directory(
    os: Os, harness: Harness, common: str | None
) -> None:
    run = dataclasses.replace(
        paths("agent"),
        worktree="/example/clone/.worktrees/agent-example",
        git_common_dir=common,
    )
    result = policy.build(
        settings(),
        HOSTS[os],
        harness=harness,
        profile=Profile.BUILD,
        account="agent",
        unsandboxed=False,
        paths=run,
    )
    if isinstance(result.detail, model.AgyPolicy):
        assert result.detail.grants == (
            "read_file(/example/home/agent/.local/share/example-tools)",
            "read_file(/example/home/agent/.local/state/example-runs/run1.tmp/gh)",
            "read_file(/example/home/agent/.local/state/example-runs/run1.tmp/docker)",
            "write_file(/example/clone/.worktrees/agent-example)",
            f"write_file({common or '/example/clone/.worktrees/agent-example/.git'})",
            "write_file(/example/home/agent/.local/state/example-runs/run1.tmp)",
            "write_file(/example/home/agent/.local/state/mise)",
            "write_file(/example/home/agent/.local/share/mise)",
        )
    else:
        expected = ["/example/clone/.worktrees/agent-example"]
        if common == "/example/clone/.git":
            expected.append("/example/clone/.git")
        expected += [
            "/example/home/agent/.local/state/example-runs/run1.tmp",
            "/example/home/agent/.cache",
            "/example/home/agent/.local/share/mise",
            "/example/home/agent/.local/state",
        ]
        if os is Os.MACOS:
            expected.append("/dev/fd")
        if isinstance(result.detail, model.CodexPolicy):
            assert result.detail.write_roots == tuple(expected)
        else:
            assert (
                dig(result.detail.settings, "sandbox", "filesystem", "allowWrite")
                == expected
            )


@pytest.mark.parametrize("os", Os)
@pytest.mark.parametrize("profile", Profile)
@pytest.mark.parametrize("common", [None, "/example/clone/.git"])
def test_claude_review_denies_writes_to_the_git_common_directory(
    os: Os, profile: Profile, common: str | None
) -> None:
    result = policy.build(
        settings(),
        HOSTS[os],
        harness=Harness.CLAUDE,
        profile=profile,
        account="agent",
        unsandboxed=False,
        paths=dataclasses.replace(paths("agent"), git_common_dir=common),
    )
    expected = []
    if profile is Profile.REVIEW:
        expected.append("/example/home/agent/Code/example.com/example-org/example-repo")
        if common is not None:
            expected.append("/example/clone/.git")
    elif common is not None:
        expected.extend(f"{common}/{entry}" for entry in ("config", "hooks", "info"))
    assert dig(claude_settings_of(result), "sandbox", "filesystem", "denyWrite") == [
        *expected,
        *denied(os, "/example/home/agent"),
    ]


@pytest.mark.parametrize("os", Os)
@pytest.mark.parametrize("profile", Profile)
@pytest.mark.parametrize("harness", Harness)
def test_dev_fd_is_writable_only_for_codex_and_claude_on_macos(
    os: Os, profile: Profile, harness: Harness
) -> None:
    result = made(os, profile, harness, "agent")
    if isinstance(result.detail, model.CodexPolicy):
        writes = result.detail.write_roots
    elif isinstance(result.detail, model.ClaudePolicy):
        writes = strings(
            dig(result.detail.settings, "sandbox", "filesystem", "allowWrite")
        )
    else:
        assert "write_file(/dev/fd)" not in result.detail.grants
        return
    assert ("/dev/fd" in writes) is (os is Os.MACOS)


@pytest.mark.parametrize("os", Os)
@pytest.mark.parametrize("entry", ["/example/host/temp", "/example/host/cache"])
@pytest.mark.parametrize("harness", [Harness.CODEX, Harness.CLAUDE])
def test_absolute_write_roots_pass_through(
    os: Os, entry: str, harness: Harness
) -> None:
    sandbox = dataclasses.replace(SANDBOX, write_roots=(entry,))
    result = made(os, Profile.BUILD, harness, "agent", config=settings(sandbox=sandbox))
    expected = [
        "/example/home/agent/Code/example.com/example-org/example-repo",
        "/example/home/agent/.local/state/example-runs/run1.tmp",
        entry,
    ]
    if os is Os.MACOS:
        expected.append("/dev/fd")
    if isinstance(result.detail, model.CodexPolicy):
        assert result.detail.write_roots == tuple(expected)
    else:
        assert (
            dig(claude_settings_of(result), "sandbox", "filesystem", "allowWrite")
            == expected
        )


@pytest.mark.parametrize("harness", Harness)
@pytest.mark.parametrize(
    ("os", "entry", "shown", "denied_path"),
    [
        (
            Os.LINUX,
            ".config/tbhbdev/secrets",
            ".config/tbhbdev/secrets",
            ".config/tbhbdev/secrets",
        ),
        (
            Os.LINUX,
            ".config",
            ".config",
            ".config/tbhbdev/secrets",
        ),
        (
            Os.LINUX,
            ".config/tbhbdev/secrets/inner",
            ".config/tbhbdev/secrets/inner",
            ".config/tbhbdev/secrets",
        ),
        (
            Os.LINUX,
            ".",
            "/example/home/agent",
            ".config/tbhbdev/secrets",
        ),
        (
            Os.MACOS,
            "/example/home/agent/Library/Keychains/tbhbdev.keychain-db",
            "/example/home/agent/Library/Keychains/tbhbdev.keychain-db",
            "/example/home/agent/Library/Keychains/tbhbdev.keychain-db",
        ),
        (
            Os.MACOS,
            "Library/Keychains",
            "/example/home/agent/Library/Keychains",
            "/example/home/agent/Library/Keychains/tbhbdev.keychain-db",
        ),
        (
            Os.MACOS,
            "Library/Keychains/tbhbdev.keychain-db/inner",
            "/example/home/agent/Library/Keychains/tbhbdev.keychain-db/inner",
            "/example/home/agent/Library/Keychains/tbhbdev.keychain-db",
        ),
        (
            Os.MACOS,
            ".",
            "/example/home/agent",
            "/example/home/agent/Library/Keychains/tbhbdev.keychain-db",
        ),
        (
            Os.MACOS,
            "Library/Keychains/tbhbdev.keychain",
            "/example/home/agent/Library/Keychains/tbhbdev.keychain",
            "/example/home/agent/Library/Keychains/tbhbdev.keychain",
        ),
        (Os.MACOS, "/private/tmp", "/private/tmp", "/private/tmp/example-sockets"),
        (Os.LINUX, "/tmp", "/tmp", "/tmp/example-sockets"),
    ],
)
def test_configured_write_roots_cannot_reach_denied_paths(
    os: Os, harness: Harness, entry: str, shown: str, denied_path: str
) -> None:
    sandbox = dataclasses.replace(SANDBOX, write_roots=(entry,))
    with pytest.raises(Invalid) as caught:
        made(os, Profile.BUILD, harness, "agent", config=settings(sandbox=sandbox))
    home = "/example/home/agent"
    shown = shown if shown.startswith("/") else f"{home}/{shown}"
    denied_path = (
        denied_path if denied_path.startswith("/") else f"{home}/{denied_path}"
    )
    assert str(caught.value) == (
        f"policy write path {shown} overlaps the denied path {denied_path}"
    )


@pytest.mark.parametrize("harness", Harness)
@pytest.mark.parametrize("os", Os)
@pytest.mark.parametrize(
    ("profile", "field"),
    [
        (Profile.BUILD, "worktree"),
        (Profile.BUILD, "tmpdir"),
        (Profile.BUILD, "git_common_dir"),
        (Profile.REVIEW, "tmpdir"),
        (Profile.REVIEW, "audit_dir"),
    ],
)
@pytest.mark.parametrize(
    "write", ["/example/denied/item", "/example/denied", "/example/denied/item/inner"]
)
def test_run_write_paths_cannot_reach_denied_paths(
    os: Os,
    profile: Profile,
    harness: Harness,
    field: Literal["worktree", "tmpdir", "git_common_dir", "audit_dir"],
    write: str,
) -> None:
    original = paths("agent")
    run = dataclasses.replace(
        original,
        worktree=write if field == "worktree" else original.worktree,
        tmpdir=write if field == "tmpdir" else original.tmpdir,
        git_common_dir=write if field == "git_common_dir" else original.git_common_dir,
        audit_dir=write if field == "audit_dir" else original.audit_dir,
    )
    config = dataclasses.replace(settings(), sockets_dir="/example/denied/item")
    with pytest.raises(Invalid) as caught:
        policy.build(
            config,
            HOSTS[os],
            harness=harness,
            profile=profile,
            account="agent",
            unsandboxed=False,
            paths=run,
        )
    assert str(caught.value) == (
        f"policy write path {write} overlaps the denied path /example/denied/item"
    )


@pytest.mark.parametrize("os", Os)
@pytest.mark.parametrize("write", [".example-denied", ".example-denied/inner"])
def test_agy_configured_write_grants_cannot_reach_denied_paths(
    os: Os, write: str
) -> None:
    agy = dataclasses.replace(SANDBOX.agy, build_write=(write,))
    config = settings(sandbox=dataclasses.replace(SANDBOX, agy=agy))
    with pytest.raises(Invalid) as caught:
        made(os, Profile.BUILD, Harness.AGY, "agent", config=config)
    assert str(caught.value) == (
        f"policy write path /example/home/agent/{write} overlaps the denied path "
        "/example/home/agent/.example-denied"
    )


@pytest.mark.parametrize("os", Os)
@pytest.mark.parametrize(
    ("harness", "profile"),
    [
        (Harness.CODEX, Profile.BUILD),
        (Harness.CLAUDE, Profile.BUILD),
        (Harness.CODEX, Profile.REVIEW),
        (Harness.CLAUDE, Profile.REVIEW),
        (Harness.AGY, Profile.REVIEW),
    ],
)
def test_unused_agy_build_write_grants_do_not_block_other_policies(
    os: Os, harness: Harness, profile: Profile
) -> None:
    agy = dataclasses.replace(SANDBOX.agy, build_write=(".example-denied",))
    config = settings(sandbox=dataclasses.replace(SANDBOX, agy=agy))
    assert made(os, profile, harness, "agent", config=config)


@pytest.mark.parametrize("harness", [Harness.CODEX, Harness.CLAUDE])
@pytest.mark.parametrize("profile", Profile)
@pytest.mark.parametrize("denied_path", ["/dev", "/dev/fd", "/dev/fd/inner"])
def test_dev_fd_cannot_reach_a_denied_path(
    harness: Harness, profile: Profile, denied_path: str
) -> None:
    config = dataclasses.replace(settings(), sockets_dir=denied_path)
    with pytest.raises(Invalid) as caught:
        made(Os.MACOS, profile, harness, "agent", config=config)
    assert str(caught.value) == (
        f"policy write path /dev/fd overlaps the denied path {denied_path}"
    )


def test_unsandboxed_still_refuses_overlapping_write_roots() -> None:
    sandbox = dataclasses.replace(SANDBOX, write_roots=(".example-denied",))
    with pytest.raises(Invalid) as caught:
        made(
            Os.LINUX,
            Profile.BUILD,
            Harness.CODEX,
            "agent",
            unsandboxed=True,
            config=settings(sandbox=sandbox),
        )
    assert str(caught.value) == (
        "policy write path /example/home/agent/.example-denied "
        "overlaps the denied path /example/home/agent/.example-denied"
    )


@pytest.mark.parametrize("os", Os)
@pytest.mark.parametrize("harness", Harness)
@pytest.mark.parametrize("profile", Profile)
@pytest.mark.parametrize(
    "write", ["/example/host/cache", "/example/home/agent/.example-denied-other"]
)
def test_unrelated_write_roots_are_allowed(
    os: Os, harness: Harness, profile: Profile, write: str
) -> None:
    sandbox = dataclasses.replace(SANDBOX, write_roots=(write,))
    assert made(os, profile, harness, "agent", config=settings(sandbox=sandbox))


@pytest.mark.parametrize("harness", Harness)
def test_build_gets_common_git_directory_and_only_its_signing_socket(
    harness: Harness,
) -> None:
    original = paths("agent")
    common = "/example/clone/git-common"
    socket = original.tmpdir + "/ssh.sock"
    run = dataclasses.replace(original, git_common_dir=common, signing_socket=socket)
    result = policy.build(
        settings(),
        LINUX,
        harness=harness,
        profile=Profile.BUILD,
        account="agent",
        unsandboxed=False,
        paths=run,
    )
    assert result.env == env_of(Os.LINUX, harness, "agent") | {"SSH_AUTH_SOCK": socket}
    if isinstance(result.detail, model.CodexPolicy):
        assert result.detail.write_roots == (
            original.worktree,
            common,
            original.tmpdir,
            original.home + "/.cache",
            original.home + "/.local/share/mise",
            original.home + "/.local/state",
        )
    elif isinstance(result.detail, model.ClaudePolicy):
        assert dig(result.detail.settings, "env", "SSH_AUTH_SOCK") == socket
        assert dig(result.detail.settings, "sandbox", "network") == {
            "allowedDomains": [],
            "allowUnixSockets": [socket],
        }
        assert common in strings(
            dig(result.detail.settings, "sandbox", "filesystem", "allowWrite")
        )
    else:
        assert f"read_file({socket})" in result.detail.grants


def test_review_never_grants_common_git_writes() -> None:
    run = dataclasses.replace(
        paths("agent"), git_common_dir="/example/clone/git-common"
    )
    result = policy.build(
        settings(),
        LINUX,
        harness=Harness.CODEX,
        profile=Profile.REVIEW,
        account="agent",
        unsandboxed=False,
        paths=run,
    )
    assert isinstance(result.detail, model.CodexPolicy)
    assert "/example/clone/git-common" not in result.detail.write_roots


@pytest.mark.parametrize("os", Os)
@pytest.mark.parametrize(
    ("profile", "socket", "network"),
    [
        (Profile.BUILD, None, {"allowedDomains": []}),
        (
            Profile.BUILD,
            "/example/signing.sock",
            {"allowedDomains": [], "allowUnixSockets": ["/example/signing.sock"]},
        ),
        (
            Profile.REVIEW,
            None,
            {
                "strictAllowlist": True,
                "allowedDomains": [
                    "github.com",
                    "api.github.com",
                    "*.githubusercontent.com",
                ],
            },
        ),
        (
            Profile.REVIEW,
            "/example/signing.sock",
            {
                "strictAllowlist": True,
                "allowedDomains": [
                    "github.com",
                    "api.github.com",
                    "*.githubusercontent.com",
                ],
                "allowUnixSockets": ["/example/signing.sock"],
            },
        ),
    ],
)
def test_claude_combines_review_network_and_signing_socket(
    os: Os, profile: Profile, socket: str | None, network: dict[str, object] | None
) -> None:
    result = policy.build(
        settings(),
        HOSTS[os],
        harness=Harness.CLAUDE,
        profile=profile,
        account="agent",
        unsandboxed=False,
        paths=dataclasses.replace(paths("agent"), signing_socket=socket),
    )
    expected = claude_expected(os, profile, "agent")
    sandbox = expected["sandbox"]
    assert isinstance(sandbox, dict)
    if network is not None:
        sandbox["network"] = network
    env = expected["env"]
    assert isinstance(env, dict)
    if socket is not None:
        env["SSH_AUTH_SOCK"] = socket
    assert claude_settings_of(result) == expected


@pytest.mark.parametrize("harness", Harness)
@pytest.mark.parametrize("profile", Profile)
@pytest.mark.parametrize("os", Os)
@pytest.mark.parametrize(
    ("socket", "denied_path"),
    [
        ("/example/denied/item", "/example/denied/item"),
        ("/example/denied/item/signing.sock", "/example/denied/item"),
        ("/example/denied", "/example/denied/item"),
        ("/tmp/example-sockets/signing.sock", "/tmp/example-sockets"),
    ],
)
def test_signing_sockets_cannot_reach_denied_paths(
    os: Os, harness: Harness, profile: Profile, socket: str, denied_path: str
) -> None:
    sandbox = dataclasses.replace(SANDBOX, denied_home_paths=("/example/denied/item",))
    with pytest.raises(Invalid) as caught:
        policy.build(
            settings(sandbox=sandbox),
            HOSTS[os],
            harness=harness,
            profile=profile,
            account="agent",
            unsandboxed=False,
            paths=dataclasses.replace(paths("agent"), signing_socket=socket),
        )
    assert str(caught.value) == (
        f"policy signing socket {socket} overlaps the denied path {denied_path}"
    )


@pytest.mark.parametrize("harness", Harness)
@pytest.mark.parametrize("profile", Profile)
@pytest.mark.parametrize("os", Os)
def test_signing_socket_grants_do_not_add_write_roots(
    os: Os, harness: Harness, profile: Profile
) -> None:
    socket = "/example/signing.sock"
    original = made(os, profile, harness, "agent")
    result = policy.build(
        settings(),
        HOSTS[os],
        harness=harness,
        profile=profile,
        account="agent",
        unsandboxed=False,
        paths=dataclasses.replace(paths("agent"), signing_socket=socket),
    )
    assert result.env == original.env | {"SSH_AUTH_SOCK": socket}
    if isinstance(result.detail, model.CodexPolicy):
        assert result.detail == original.detail
    elif isinstance(result.detail, model.ClaudePolicy):
        assert dig(result.detail.settings, "sandbox", "filesystem") == dig(
            claude_settings_of(original), "sandbox", "filesystem"
        )
    else:
        assert isinstance(original.detail, model.AgyPolicy)
        reads = tuple(g for g in original.detail.grants if g.startswith("read_file("))
        writes = tuple(g for g in original.detail.grants if g.startswith("write_file("))
        assert result.detail.grants == (*reads, f"read_file({socket})", *writes)


@pytest.mark.parametrize("os", Os)
@pytest.mark.parametrize("profile", Profile)
@pytest.mark.parametrize("common", [None, "/example/clone/.git"])
def test_codex_protects_git_configuration_inside_build_write_root(
    os: Os, profile: Profile, common: str | None
) -> None:
    result = policy.build(
        settings(),
        HOSTS[os],
        harness=Harness.CODEX,
        profile=profile,
        account="agent",
        unsandboxed=False,
        paths=dataclasses.replace(paths("agent"), git_common_dir=common),
    )
    assert isinstance(result.detail, model.CodexPolicy)
    expected = (":root",)
    if profile is Profile.BUILD and common is not None:
        expected += tuple(f"{common}/{entry}" for entry in ("config", "hooks", "info"))
        assert common in result.detail.write_roots
    assert result.detail.read_roots == expected


@pytest.mark.parametrize("profile", Profile)
@pytest.mark.parametrize("reason", [None, "isolated test machine"])
def test_agy_requires_named_opt_out(profile: Profile, reason: str | None) -> None:
    configured = settings(
        sandbox=dataclasses.replace(SANDBOX, agy_unsandboxed_opt_out=reason)
    )
    if reason is None:
        with pytest.raises(Invalid) as caught:
            made(Os.LINUX, profile, Harness.AGY, "agent", config=configured)
        assert str(caught.value) == (
            "agy has no OS sandbox; configure agy_unsandboxed_opt_out "
            "with a named reason"
        )
    else:
        assert isinstance(
            made(Os.LINUX, profile, Harness.AGY, "agent", config=configured).detail,
            model.AgyPolicy,
        )


@pytest.mark.parametrize("reason", [None, "explicit network exception"])
@pytest.mark.parametrize("domains", [(), ("api.github.com", "example.com")])
@pytest.mark.parametrize("profile", Profile)
def test_codex_proxy_default_and_opt_out(
    reason: str | None, domains: tuple[str, ...], profile: Profile
) -> None:
    configured = settings(
        sandbox=dataclasses.replace(
            SANDBOX, network_domains=domains, codex_network_proxy_opt_out=reason
        )
    )
    detail = made(Os.LINUX, profile, Harness.CODEX, "agent", config=configured).detail
    assert isinstance(detail, model.CodexPolicy)
    assert detail.network_proxy == (
        None
        if reason
        else {
            "enabled": True,
            "mode": "limited",
            "domains": dict.fromkeys(domains, "allow"),
            "unix_sockets": {},
            "dangerously_allow_all_unix_sockets": False,
            "allow_local_binding": False,
        }
    )


@pytest.mark.parametrize("os", Os)
@pytest.mark.parametrize("socket", [None, "/example/signing.sock"])
def test_claude_build_network_domains(os: Os, socket: str | None) -> None:
    configured = settings(
        sandbox=dataclasses.replace(SANDBOX, network_domains=("api.github.com",))
    )
    result = policy.build(
        configured,
        HOSTS[os],
        harness=Harness.CLAUDE,
        profile=Profile.BUILD,
        account="agent",
        unsandboxed=False,
        paths=dataclasses.replace(paths("agent"), signing_socket=socket),
    )
    assert isinstance(result.detail, model.ClaudePolicy)
    assert dig(result.detail.settings, "sandbox", "network") == {
        "allowedDomains": ["api.github.com"],
        **({"allowUnixSockets": [socket]} if socket else {}),
    }


@pytest.mark.parametrize(
    "field", ["worktree", "git_common_dir", "tmpdir", "audit_dir", "configured", "fd"]
)
@pytest.mark.parametrize("relation", ["same", "ancestor", "descendant", "sibling"])
def test_host_protected_paths_refuse_both_directions(field: str, relation: str) -> None:
    run = model.RunPaths(
        "/example/home",
        "/one/worktree",
        "/three/temp",
        "/three/temp/codex",
        "/four/audit",
        "/two/common",
    )
    configured = settings(sandbox=dataclasses.replace(SANDBOX, write_roots=(".cache",)))
    write = {"configured": run.home + "/.cache", "fd": "/dev/fd"}.get(field) or getattr(
        run, field
    )
    protected = {
        "same": write,
        "ancestor": str(PurePosixPath(write).parent),
        "descendant": write + "/bin",
        "sibling": write + "-other",
    }[relation]
    if relation == "sibling":
        policy.protect_host(configured, run, (protected,))
    else:
        with pytest.raises(Invalid) as caught:
            policy.protect_host(configured, run, (protected,))
        assert str(caught.value) == (
            f"protected host path {protected} overlaps write root {write}; "
            "narrow that write root"
        )


@pytest.mark.parametrize("harness", Harness)
def test_linux_socket_denies_use_the_run_uid(harness: Harness) -> None:
    run = dataclasses.replace(paths("agent"), uid=12345)
    result = policy.build(
        settings(),
        LINUX,
        harness=harness,
        profile=Profile.BUILD,
        account="agent",
        unsandboxed=False,
        paths=run,
    )
    assert "/run/user/12345" in result.deny
    assert "/run/user/0" not in result.deny
    for path in ("/run/WSL", "/var/run/docker.sock", "/run/docker.sock"):
        assert path in result.deny


def test_host_protection_handles_a_missing_common_directory() -> None:
    policy.protect_host(settings(), paths("agent"), ("/example/installation",))

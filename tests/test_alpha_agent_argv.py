"""The argv builders are tables over literal values: no process, no file, no secret."""

# ruff: noqa: S108 -- the paths are test data under /tmp that no test creates

from __future__ import annotations

import dataclasses
import itertools
import json
import tomllib

import pytest
from agent_example import (
    DENIED,
    LINUX,
    LINUX_SOCKET_DENIES,
    MACOS,
    MARKER,
    TOKEN_DIR,
    paths,
    settings,
)

from tbhbdev.alpha.agent import model
from tbhbdev.alpha.agent.decisions import argv, policy
from tbhbdev.alpha.agent.model import Harness, Invalid, Os, Profile, RunKind

HOME = "/example/home/agent"
TMP = f"{HOME}/.local/state/example-runs/run1.tmp"
WORKTREE = f"{HOME}/Code/example.com/example-org/example-repo"
BRIEF_FILE = f"{TMP}/brief.md"
BRIEF = "Example brief text: change the example."
PROJECT = "00000000-0000-4000-8000-000000000000"
POINTER = f"Read the brief in {BRIEF_FILE} and carry it out."
COMMIT = "0123456789abcdef0123456789abcdef01234567"
COMBINATIONS = list(itertools.product(Harness, RunKind, Profile))
IDS = [f"{h.value}-{k.value}-{p.value}" for h, k, p in COMBINATIONS]

DENY_LINUX = (
    f"{HOME}/{TOKEN_DIR}",
    *LINUX_SOCKET_DENIES,
    "/tmp/example-sockets",
    f"{HOME}/{DENIED}",
)
HANDOFF = (
    "-c",
    f'shell_environment_policy.set.DOCKER_CONFIG="{TMP}/docker"',
    "-c",
    f'shell_environment_policy.set.GH_CONFIG_DIR="{TMP}/gh"',
    "-c",
    f'shell_environment_policy.set.TMPDIR="{TMP}"',
    "-c",
    f'shell_environment_policy.set.TMPPREFIX="{TMP}/zsh"',
)
CODEX_HANDOFF = (
    "-c",
    f'shell_environment_policy.set.CODEX_HOME="{TMP}/codex"',
    *HANDOFF,
)
CODEX_TAIL = (
    "-c",
    "sandbox_workspace_write.network_access=true",
    "-c",
    'approval_policy="never"',
    "-c",
    'web_search="live"',
    "-c",
    'features.network_proxy={enabled=true,mode="limited",domains={},unix_sockets={},dangerously_allow_all_unix_sockets=false,allow_local_binding=false}',
)
CODEX_BUILD_FS = (
    '{filesystem = {":root" = "read", '
    f'"{WORKTREE}/.git/config" = "read", "{WORKTREE}/.git/hooks" = "read", '
    f'"{WORKTREE}/.git/info" = "read", '
    f'"{WORKTREE}" = "write", "{TMP}" = "write", '
    f'"{HOME}/.cache" = "write", "{HOME}/.local/share/mise" = "write", '
    f'"{HOME}/.local/state" = "write", '
    f'"{HOME}/{TOKEN_DIR}" = "none", '
    '"/run/WSL" = "none", "/var/run/docker.sock" = "none", '
    '"/run/docker.sock" = "none", "/run/user/0" = "none", '
    '"/tmp/example-sockets" = "none", '
    f'"{HOME}/{DENIED}" = "none"}}, network = {{enabled = true}}}}'
)
CODEX_REVIEW_FS = (
    '{filesystem = {":root" = "read", '
    f'"{TMP}" = "write", "{HOME}/.local/state/example-audit" = "write", '
    f'"{HOME}/{TOKEN_DIR}" = "none", '
    '"/run/WSL" = "none", "/var/run/docker.sock" = "none", '
    '"/run/docker.sock" = "none", "/run/user/0" = "none", '
    '"/tmp/example-sockets" = "none", '
    f'"{HOME}/{DENIED}" = "none"}}, network = {{enabled = true}}}}'
)
MODEL = ("-m", "example-model-1", "-c", 'model_reasoning_effort="high"')


def codex_sandbox(profile: Profile) -> tuple[str, ...]:
    table = CODEX_BUILD_FS if profile is Profile.BUILD else CODEX_REVIEW_FS
    return (
        "-c",
        f'default_permissions="{profile.value}"',
        "-c",
        f"permissions.{profile.value}={table}",
    )


def claude_settings_of(profile: Profile) -> dict[str, object]:
    detail = policy_for(Harness.CLAUDE, profile).detail
    assert isinstance(detail, model.ClaudePolicy)
    return detail.settings


def claude_settings(profile: Profile) -> str:
    return json.dumps(
        claude_settings_of(profile), separators=(",", ":"), sort_keys=True
    )


def request(
    harness: Harness,
    kind: RunKind,
    profile: Profile,
    *,
    unsandboxed: bool = False,
    timeout: int | None = None,
) -> model.RunRequest:
    claude = harness is Harness.CLAUDE
    agy_exec = harness is Harness.AGY and kind is RunKind.EXEC
    return model.RunRequest(
        kind=kind,
        repo="example-org/example-repo",
        commit=COMMIT if profile is Profile.REVIEW else None,
        harness=harness,
        model="example-model-1",
        effort="high",
        profile=profile,
        claude_profile="primary" if claude else None,
        reason="example-gap" if claude else None,
        account="agent",
        timeout=timeout if timeout is not None else (900 if agy_exec else None),
        class_=None,
        variant=None,
        role=None,
        unsandboxed=unsandboxed,
        authorized_by="example-org/example-repo#1" if unsandboxed else None,
        startup_deadline_seconds=120,
    )


def policy_for(
    harness: Harness,
    profile: Profile,
    *,
    os: Os = Os.LINUX,
    unsandboxed: bool = False,
    config: model.Settings | None = None,
) -> model.Policy:
    return policy.build(
        config or settings(),
        LINUX if os is Os.LINUX else MACOS,
        harness=harness,
        profile=profile,
        account="agent",
        unsandboxed=unsandboxed,
        paths=paths("agent"),
    )


def built(
    harness: Harness,
    kind: RunKind,
    profile: Profile,
    *,
    config: model.Settings | None = None,
    os: Os = Os.LINUX,
) -> tuple[str, ...]:
    return argv.harness(
        request(harness, kind, profile),
        policy_for(harness, profile, os=os, config=config),
        worktree=WORKTREE,
        brief_file=BRIEF_FILE,
        agy_project=PROJECT,
        agy_brief=BRIEF,
    )


def expected(harness: Harness, kind: RunKind, profile: Profile) -> tuple[str, ...]:
    headless = kind is RunKind.EXEC
    review = profile is Profile.REVIEW
    if harness is Harness.CODEX:
        return (
            "codex",
            *(("exec", "--skip-git-repo-check") if headless else ()),
            *(("--ephemeral",) if headless and review else ()),
            "--strict-config",
            "-C",
            WORKTREE,
            *codex_sandbox(profile),
            "-c",
            f'projects={{"{WORKTREE}"={{trust_level="untrusted"}}}}',
            *CODEX_TAIL,
            *CODEX_HANDOFF,
            *MODEL,
            "-" if headless else POINTER,
        )
    if harness is Harness.CLAUDE:
        return (
            "claude",
            *(("-p",) if headless else ()),
            "--model",
            "example-model-1",
            "--effort",
            "high",
            "--permission-mode",
            "dontAsk" if review else "auto",
            "--settings",
            claude_settings(profile),
            "--setting-sources",
            "user",
            "--strict-mcp-config",
            *(() if headless else (POINTER,)),
        )
    head = (
        "agy" if review else "agy-agent",
        "--project",
        PROJECT,
        "--model",
        "example-model-1",
        *(("--mode", "plan", "--sandbox") if review else ()),
    )
    if not headless:
        return (*head, "--prompt-interactive", POINTER)
    note = f"Run shell commands in {WORKTREE} unless the task names another directory."
    text = f"\n{BRIEF}" if review else f"{note}\n\n{BRIEF}"
    return (*head, "--print-timeout", "900s", "--print", text)


@pytest.mark.parametrize(("harness", "kind", "profile"), COMBINATIONS, ids=IDS)
def test_harness_argv(harness: Harness, kind: RunKind, profile: Profile) -> None:
    assert built(harness, kind, profile) == expected(harness, kind, profile)


@pytest.mark.parametrize(("kind", "profile"), [*itertools.product(RunKind, Profile)])
def test_codex_ephemeral_only_for_a_headless_review(
    kind: RunKind, profile: Profile
) -> None:
    result = built(Harness.CODEX, kind, profile)
    wanted = kind is RunKind.EXEC and profile is Profile.REVIEW
    assert ("--ephemeral" in result) is wanted


def test_a_codex_build_run_is_not_ephemeral_and_a_review_is() -> None:
    assert "--ephemeral" not in built(Harness.CODEX, RunKind.EXEC, Profile.BUILD)
    assert "--ephemeral" in built(Harness.CODEX, RunKind.EXEC, Profile.REVIEW)


@pytest.mark.parametrize("profile", Profile)
def test_the_codex_profile_is_valid_toml_with_the_policy_entries(
    profile: Profile,
) -> None:
    result = built(Harness.CODEX, RunKind.EXEC, profile)
    flag = f"permissions.{profile.value}="
    value = next(a for a in result if a.startswith(flag)).removeprefix(flag)
    parsed = tomllib.loads(f"profile = {value}")["profile"]
    fs = parsed["filesystem"]
    assert parsed["network"] == {"enabled": True}
    assert [p for p, mode in fs.items() if mode == "none"] == list(DENY_LINUX)
    assert fs[":root"] == "read"
    writes = [p for p, mode in fs.items() if mode == "write"]
    assert (WORKTREE in writes) is (profile is Profile.BUILD)


def test_macos_codex_denies_the_private_alias_too() -> None:
    result = built(Harness.CODEX, RunKind.EXEC, Profile.BUILD, os=Os.MACOS)
    value = next(a for a in result if a.startswith("permissions.build="))
    fs = tomllib.loads(f"profile = {value.removeprefix('permissions.build=')}")
    denies = [p for p, m in fs["profile"]["filesystem"].items() if m == "none"]
    assert denies == [
        f"{HOME}/Library/Keychains/tbhbdev.keychain-db",
        f"{HOME}/Library/Keychains/tbhbdev.keychain",
        "/tmp/example-sockets",
        "/private/tmp/example-sockets",
        f"{HOME}/{DENIED}",
    ]


@pytest.mark.parametrize("kind", RunKind)
def test_unsandboxed_codex_is_full_access_with_no_profile(kind: RunKind) -> None:
    pol = policy_for(Harness.CODEX, Profile.BUILD, unsandboxed=True)
    req = request(Harness.CODEX, kind, Profile.BUILD, unsandboxed=True)
    result = argv.harness(req, pol, worktree=WORKTREE, brief_file=BRIEF_FILE)
    assert result == (
        "codex",
        *(("exec", "--skip-git-repo-check") if kind is RunKind.EXEC else ()),
        "--strict-config",
        "-C",
        WORKTREE,
        "-s",
        "danger-full-access",
        "-c",
        f'projects={{"{WORKTREE}"={{trust_level="untrusted"}}}}',
        *CODEX_TAIL,
        *CODEX_HANDOFF,
        *MODEL,
        "-" if kind is RunKind.EXEC else POINTER,
    )


def test_codex_project_trust_preserves_path_as_one_toml_key() -> None:
    workspace = '/example/path.with.dots/and "quotes"'
    command = argv.harness(
        request(Harness.CODEX, RunKind.EXEC, Profile.BUILD),
        policy_for(Harness.CODEX, Profile.BUILD),
        worktree=workspace,
        brief_file=BRIEF_FILE,
    )
    entry = next(value for value in command if value.startswith("projects="))
    assert tomllib.loads(entry) == {
        "projects": {workspace: {"trust_level": "untrusted"}}
    }


@pytest.mark.parametrize(
    ("profile", "build", "review", "wanted"),
    [
        (Profile.BUILD, "cached", "disabled", "cached"),
        (Profile.REVIEW, "cached", "disabled", "disabled"),
        (Profile.BUILD, "disabled", "live", "disabled"),
        (Profile.REVIEW, "disabled", "live", "live"),
    ],
)
def test_web_search_is_narrowed_per_profile(
    profile: Profile, build: str, review: str, wanted: str
) -> None:
    codex = model.Codex(model.WebSearch(build), model.WebSearch(review))
    result = built(Harness.CODEX, RunKind.EXEC, profile, config=settings(codex=codex))
    assert result[result.index(f'web_search="{wanted}"') - 1] == "-c"
    assert sum(a.startswith("web_search=") for a in result) == 1


@pytest.mark.parametrize("profile", Profile)
def test_claude_settings_argv_is_the_policy_settings(profile: Profile) -> None:
    result = built(Harness.CLAUDE, RunKind.EXEC, profile)
    parsed = json.loads(result[result.index("--settings") + 1])
    assert parsed == claude_settings_of(profile)
    assert parsed["sandbox"]["filesystem"]["denyRead"] == list(DENY_LINUX)


def test_a_brief_is_in_argv_only_for_an_agy_exec_run() -> None:
    for harness, kind, profile in COMBINATIONS:
        result = built(harness, kind, profile)
        carries = harness is Harness.AGY and kind is RunKind.EXEC
        assert any(BRIEF in a for a in result) is carries
        assert (BRIEF_FILE in " ".join(result)) is (kind is RunKind.START)


def test_the_brief_is_one_argument_after_print() -> None:
    for profile in Profile:
        result = built(Harness.AGY, RunKind.EXEC, profile)
        assert result[-2] == "--print"
        assert BRIEF in result[-1]
        assert sum(BRIEF in a for a in result) == 1


@pytest.mark.parametrize("brief", ["- item", "--- front matter", "--project", ""])
@pytest.mark.parametrize("profile", Profile)
def test_agy_brief_cannot_be_a_flag(brief: str, profile: Profile) -> None:
    result = argv.harness(
        request(Harness.AGY, RunKind.EXEC, profile),
        policy_for(Harness.AGY, profile),
        worktree=WORKTREE,
        brief_file=BRIEF_FILE,
        agy_project=PROJECT,
        agy_brief=brief,
    )
    note = f"Run shell commands in {WORKTREE} unless the task names another directory."
    assert result[-2:] == (
        "--print",
        f"\n{brief}" if profile is Profile.REVIEW else f"{note}\n\n{brief}",
    )


@pytest.mark.parametrize("os", Os)
@pytest.mark.parametrize("profile", Profile)
def test_codex_tables_have_no_repeated_paths(os: Os, profile: Profile) -> None:
    original = settings()
    sandbox = dataclasses.replace(
        original.sandbox,
        denied_home_paths=(DENIED, TOKEN_DIR, DENIED),
        write_roots=(".local/state/example-runs/run1.tmp", ".cache", ".cache"),
    )
    config = dataclasses.replace(
        original, sandbox=sandbox, sockets_dir=f"{HOME}/{DENIED}"
    )
    result = built(Harness.CODEX, RunKind.EXEC, profile, os=os, config=config)
    flag = f"permissions.{profile.value}="
    value = next(a for a in result if a.startswith(flag)).removeprefix(flag)
    fs = tomllib.loads(f"profile = {value}")["profile"]["filesystem"]
    deny = (
        [
            f"{HOME}/{TOKEN_DIR}",
            *LINUX_SOCKET_DENIES,
            f"{HOME}/{DENIED}",
        ]
        if os is Os.LINUX
        else [
            f"{HOME}/Library/Keychains/tbhbdev.keychain-db",
            f"{HOME}/Library/Keychains/tbhbdev.keychain",
            f"{HOME}/{DENIED}",
            f"{HOME}/{TOKEN_DIR}",
        ]
    )
    assert [p for p, mode in fs.items() if mode == "none"] == deny
    writes = (
        [WORKTREE, TMP, f"{HOME}/.cache"]
        if profile is Profile.BUILD
        else [TMP, f"{HOME}/.local/state/example-audit"]
    )
    if os is Os.MACOS:
        writes.append("/dev/fd")
    assert [p for p, mode in fs.items() if mode == "write"] == writes


def test_agy_timeout_is_whole_seconds() -> None:
    req = request(Harness.AGY, RunKind.EXEC, Profile.REVIEW, timeout=3600)
    result = argv.harness(
        req,
        policy_for(Harness.AGY, Profile.REVIEW),
        worktree=WORKTREE,
        brief_file=BRIEF_FILE,
        agy_project=PROJECT,
        agy_brief=BRIEF,
    )
    assert result[result.index("--print-timeout") + 1] == "3600s"


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        (
            {"agy_project": None, "agy_brief": BRIEF},
            "argv agy run requires agy_project",
        ),
        (
            {"agy_project": PROJECT, "agy_brief": None},
            "argv agy exec run requires agy_brief",
        ),
    ],
)
def test_an_agy_exec_run_needs_its_project_and_brief(
    kwargs: dict[str, str | None], message: str
) -> None:
    with pytest.raises(Invalid) as caught:
        argv.harness(
            request(Harness.AGY, RunKind.EXEC, Profile.BUILD),
            policy_for(Harness.AGY, Profile.BUILD),
            worktree=WORKTREE,
            brief_file=BRIEF_FILE,
            agy_project=kwargs["agy_project"],
            agy_brief=kwargs["agy_brief"],
        )
    assert str(caught.value) == message


def test_an_agy_exec_run_needs_a_timeout() -> None:
    base = request(Harness.AGY, RunKind.EXEC, Profile.BUILD)
    req = dataclasses.replace(base, timeout=None)
    with pytest.raises(Invalid) as caught:
        argv.harness(
            req,
            policy_for(Harness.AGY, Profile.BUILD),
            worktree=WORKTREE,
            brief_file=BRIEF_FILE,
            agy_project=PROJECT,
            agy_brief=BRIEF,
        )
    assert str(caught.value) == "argv agy exec run requires a timeout"


def test_an_agy_start_run_needs_only_its_project() -> None:
    result = argv.harness(
        request(Harness.AGY, RunKind.START, Profile.BUILD),
        policy_for(Harness.AGY, Profile.BUILD),
        worktree=WORKTREE,
        brief_file=BRIEF_FILE,
        agy_project=PROJECT,
    )
    assert result[-2:] == ("--prompt-interactive", POINTER)


@pytest.mark.parametrize(
    ("harness", "profile"),
    [
        (Harness.CLAUDE, Profile.BUILD),
        (Harness.CODEX, Profile.REVIEW),
        (Harness.CLAUDE, Profile.REVIEW),
    ],
)
def test_a_policy_for_another_harness_or_profile_is_refused(
    harness: Harness, profile: Profile
) -> None:
    with pytest.raises(Invalid) as caught:
        argv.harness(
            request(Harness.CODEX, RunKind.EXEC, Profile.BUILD),
            policy_for(harness, profile),
            worktree=WORKTREE,
            brief_file=BRIEF_FILE,
        )
    assert str(caught.value) == (
        "argv policy was built for another harness or profile than the request's"
    )


def test_ssh() -> None:
    result = argv.ssh(
        LINUX,
        ("example-tool", "run", "a b"),
        connect_timeout=15,
        keepalive=30,
    )
    assert result == (
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=15",
        "-o",
        "ServerAliveInterval=30",
        "example-linux",
        "bash -lc 'example-tool run '\"'\"'a b'\"'\"''",
    )


def test_ssh_uses_the_hosts_shell() -> None:
    result = argv.ssh(MACOS, ("true",), connect_timeout=1, keepalive=2)
    assert result[-2:] == ("example-macos", "zsh -lc true")


def test_tmux() -> None:
    result = argv.tmux("/example/sockets", "run-1", ("example-tool", "go"))
    assert result == (
        "tmux",
        "-S",
        "/example/sockets/agents",
        "new-session",
        "-d",
        "-s",
        "ag-run-1",
        "example-tool",
        "go",
    )


@pytest.mark.parametrize("name", ["", "-a", "a b", "a;b", "a/b", "x" * 65])
def test_tmux_refuses_a_name_that_is_not_a_run_name(name: str) -> None:
    with pytest.raises(Invalid) as caught:
        argv.tmux("/example/sockets", name, ("true",))
    assert str(caught.value) == (
        f"tmux session name {name!r} must match [A-Za-z0-9][A-Za-z0-9_-]{{0,63}}"
    )


def test_no_argv_holds_a_secret_reference_or_a_token() -> None:
    config = settings(reference=f"{MARKER}/{MARKER}/{MARKER}")
    every = [
        built(harness, kind, profile, config=config)
        for harness, kind, profile in COMBINATIONS
    ]
    every.append(argv.ssh(LINUX, ("example-tool",), connect_timeout=1, keepalive=2))
    every.append(argv.tmux("/example/sockets", "run-1", every[0]))
    assert len(every) == len(COMBINATIONS) + 2
    for command in every:
        assert not any(MARKER in element for element in command)


def test_codex_overrides_the_signing_socket_setting() -> None:
    original = policy_for(Harness.CODEX, Profile.BUILD)
    pol = dataclasses.replace(
        original, env=original.env | {"SSH_AUTH_SOCK": "/example/private/ssh.sock"}
    )
    result = argv.harness(
        request(Harness.CODEX, RunKind.EXEC, Profile.BUILD),
        pol,
        worktree=WORKTREE,
        brief_file=BRIEF_FILE,
    )
    flag = 'shell_environment_policy.set.SSH_AUTH_SOCK="/example/private/ssh.sock"'
    assert result[result.index(flag) - 1] == "-c"
    assert result.count(flag) == 1

    baseline = argv.harness(
        request(Harness.CODEX, RunKind.EXEC, Profile.BUILD),
        original,
        worktree=WORKTREE,
        brief_file=BRIEF_FILE,
    )
    index = result.index(flag)
    assert result[: index - 1] + result[index + 1 :] == baseline


@pytest.mark.parametrize("opt_out", [False, True])
def test_network_proxy_overrides_are_explicit(opt_out: bool) -> None:
    original = settings()
    configured = dataclasses.replace(
        original,
        sandbox=dataclasses.replace(
            original.sandbox,
            network_domains=("api.github.com", "example.com"),
            codex_network_proxy_opt_out="test exception" if opt_out else None,
        ),
    )
    result = built(Harness.CODEX, RunKind.EXEC, Profile.BUILD, config=configured)
    assert result[:3] == ("codex", "exec", "--skip-git-repo-check")
    setting = next(arg for arg in result if arg.startswith("features.network_proxy"))
    assert result[result.index(setting) - 1] == "-c"
    parsed = tomllib.loads(setting)["features"]["network_proxy"]
    assert parsed == (
        {"enabled": False}
        if opt_out
        else {
            "enabled": True,
            "mode": "limited",
            "domains": {"api.github.com": "allow", "example.com": "allow"},
            "unix_sockets": {},
            "dangerously_allow_all_unix_sockets": False,
            "allow_local_binding": False,
        }
    )


@pytest.mark.parametrize("harness", [Harness.CODEX, Harness.CLAUDE])
@pytest.mark.parametrize("kind", RunKind)
@pytest.mark.parametrize("schema", [None, '{"type":"object"}'])
def test_schema_options_are_only_for_exec(
    harness: Harness, kind: RunKind, schema: str | None
) -> None:
    req = request(harness, kind, Profile.BUILD)
    pol = policy_for(harness, Profile.BUILD)
    baseline = argv.harness(req, pol, worktree=WORKTREE, brief_file=BRIEF_FILE)
    path = "/example/schema.json" if schema is not None else None
    result = argv.harness(
        req,
        pol,
        worktree=WORKTREE,
        brief_file=BRIEF_FILE,
        schema_file=path,
        schema_text=schema,
    )
    if kind is RunKind.EXEC and schema is not None:
        expected = (
            (*baseline[:-1], "--output-schema", path, baseline[-1])
            if harness is Harness.CODEX
            else (*baseline, "--output-format", "json", "--json-schema", schema)
        )
        assert result == expected
    else:
        assert result == baseline

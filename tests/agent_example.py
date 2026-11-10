"""Example agent settings, hosts and run paths for the policy and argv tests."""

# ruff: noqa: S108 -- the paths are test data under /tmp that no test creates

from __future__ import annotations

from tbhbdev import secrets
from tbhbdev.alpha.agent import model

MARKER = "marker-secret-reference-0451"
# The Connect token store's directory and its first part, as secrets.py has them.
TOKEN_DIR = str(secrets.LINUX_TOKEN_FILE.parent)
TOKEN_TOP = secrets.LINUX_TOKEN_FILE.parts[0]
# The fixture's one denied home path.
DENIED = ".example-denied"
LINUX_SOCKET_DENIES = (
    "/run/WSL",
    "/var/run/docker.sock",
    "/run/docker.sock",
    "/run/user/0",
)

SANDBOX = model.Sandbox(
    agy_unsandboxed_opt_out="test grants only",
    codex_unsandboxed_opt_out="isolated test host",
    denied_home_paths=(DENIED,),
    agy=model.AgySandbox(
        review_read=(".local/share/example-gh", ".local/share/example-mise"),
        build_read=(".local/share/example-tools",),
        build_write=(".local/state/mise", ".local/share/mise"),
    ),
    audit_dir=".local/state/example-audit",
    write_roots=(".cache", ".local/share/mise", ".local/state"),
    path=model.SandboxPath(
        linux=(".local/share/mise/shims", "/usr/local/bin", "/usr/bin"),
        macos=(".local/share/mise/shims", "/opt/example/bin", "/usr/bin"),
    ),
)

LIVE = model.Codex(model.WebSearch.LIVE, model.WebSearch.LIVE)
LINUX = model.Host(  # noqa: S604 -- a config key, not a subprocess flag
    name="example-linux",
    os=model.Os.LINUX,
    shell="bash",
    account="agent",
    unsandboxed=True,
    accounts=("agent", "ops"),
)
MACOS = model.Host(  # noqa: S604 -- a config key, not a subprocess flag
    name="example-macos",
    os=model.Os.MACOS,
    shell="zsh",
    account="agent",
    unsandboxed=False,
    accounts=("agent", "ops"),
)


def settings(
    *,
    sandbox: model.Sandbox = SANDBOX,
    codex: model.Codex = LIVE,
    reference: str = "example-vault/example-item/field",
) -> model.Settings:
    """Settings whose every secret reference is REFERENCE."""
    return model.Settings(
        launch_wait_seconds=120,
        agy_timeout_seconds=900,
        kill_grace_seconds=10,
        max_run_minutes=None,
        sockets_dir="/tmp/example-sockets",
        sandbox=sandbox,
        codex=codex,
        credentials=tuple(
            model.Credentials(
                account=account,
                gh=reference,
                docker=reference,
                reviews_app_id=reference,
                reviews_app_client_id=reference,
                reviews_app_pem=reference,
                signing=reference,
            )
            for account in ("agent", "ops")
        ),
        claude=model.Claude(
            "primary", (model.ClaudeProfile("primary", reference, reference),)
        ),
        hosts=(LINUX, MACOS),
    )


def paths(account: str) -> model.RunPaths:
    """The run paths of ACCOUNT's home."""
    home = f"/example/home/{account}"
    tmpdir = f"{home}/.local/state/example-runs/run1.tmp"
    return model.RunPaths(
        home=home,
        worktree=f"{home}/Code/example.com/example-org/example-repo",
        tmpdir=tmpdir,
        codex_home=f"{tmpdir}/codex",
        audit_dir=f"{home}/.local/state/example-audit",
    )

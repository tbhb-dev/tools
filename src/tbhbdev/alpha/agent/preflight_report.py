"""Collect independent host checks without inventing missing prerequisites."""

from __future__ import annotations

import dataclasses
import re
import subprocess  # nosec B404
from contextlib import ExitStack
from pathlib import Path
from typing import TYPE_CHECKING

from tbhbdev import userconfig
from tbhbdev.alpha.agent import credentials, host_tools, runfiles, settings
from tbhbdev.alpha.agent.model import Harness, Invalid
lazy from tbhbdev.alpha.agent import run_host

if TYPE_CHECKING:
    import argparse
    from collections.abc import Callable

PROTECTIONS = (
    "host protection",
    "clone protection",
    "codex protection",
    "executable protection",
)
ORDER = (
    "config load",
    "host and account",
    "mirror and clone",
    "workspace",
    *PROTECTIONS,
    "policy",
    "network proxy",
    "credential delivery",
    "audit directory",
    "codex auth copy",
    "harness command",
    ".bazel_env",
    "sandbox",
)


class NeedsError(Exception):
    """A check cannot run without a successful prerequisite."""


class SandboxError(Invalid):
    """Sandbox diagnostics, sanitized before they reach the log."""


@dataclasses.dataclass(frozen=True)
class Outcome[T]:
    name: str
    values: tuple[T, ...] = ()

    def get(self) -> T:
        if not self.values:
            raise NeedsError(self.name)
        return self.values[0]


class Checks:
    def __init__(self, files: runfiles.RunFiles | None = None) -> None:
        self.files = files
        self.results: dict[str, str] = {}
        self.version: str | None = None
        self.hook_tools: str | None = None

    def require(self, *names: str) -> None:
        for name in names:
            if self.files is not None and self.results.get(name) != "ok":
                raise NeedsError(name)

    def check[T](self, name: str, operation: Callable[[], T]) -> Outcome[T]:
        if self.files is None:
            return Outcome(name, (operation(),))
        try:
            result = operation()
        except NeedsError as exc:
            self.skip(name, str(exc))
        except SandboxError as exc:
            self.results[name] = (
                f"failed: {str(exc).splitlines()[0] if str(exc) else ''}"
            )
        except (
            OSError,
            ValueError,
            subprocess.SubprocessError,
            userconfig.ConfigError,
        ):
            self.results[name] = f"failed: run preparation failed at {name}"
        else:
            self.results[name] = "ok"
            return Outcome(name, (result,))
        return Outcome(name)

    def skip(self, name: str, need: str) -> None:
        self.results[name] = f"skipped: needs {need}"

    def write(self) -> int:
        if self.files is None:
            message = "report"
            raise NeedsError(message)
        files = self.files
        if self.version is not None:
            files.write("log", f"preflight: codex {self.version}\n")
        for name in ORDER:
            line = f"preflight: {name}: {self.results[name]}"
            if name == ".bazel_env" and self.hook_tools is not None:
                line = f"preflight: .bazel_env {self.hook_tools}"
            files.write("log", line[:299] + "\n")
        failed = sum(value.startswith("failed:") for value in self.results.values())
        skipped = sum(value.startswith("skipped:") for value in self.results.values())
        files.write(
            "log",
            f"preflight: {len(self.results)} checks, "
            f"{failed} failed, {skipped} skipped\n",
        )
        return int(failed > 0)


def version(executable: str, run: run_host.Run) -> str:
    try:
        output = subprocess.check_output(  # nosec B603
            [executable, "--version"],
            env={
                "HOME": str(run.tmpdir),
                "CODEX_HOME": str(run.tmpdir),
                "PATH": host_tools.search(),
            },
            cwd=run.tmpdir,
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=10,
        )
    except OSError, ValueError, subprocess.SubprocessError:
        return "version unknown"
    found = re.fullmatch(r"codex-cli (\d+\.\d+\.\d+)\s*", output)
    return found[1] if found is not None else "version unknown"


def load(
    args: argparse.Namespace,
    tmpdir: Path,
    files: runfiles.RunFiles,
    loader: Callable[
        [argparse.Namespace, Path, run_host.Settings | None], tuple[run_host.Run, str]
    ],
) -> int:
    checks = Checks(files)
    loaded = checks.check("config load", settings.load)
    pair = checks.check("host and account", lambda: loader(args, tmpdir, loaded.get()))
    if not pair.values:
        for name in ORDER[2:]:
            checks.skip(name, "host and account")
        rc = checks.write()
        files.write("rc", f"{rc}\n")
        return rc
    run, text = pair.get()
    with host_tools.configured(run.settings, run.host.os), ExitStack() as stack:
        return execute(run, text, stack)


def execute(run: run_host.Run, text: str, stack: ExitStack) -> int:
    checks = Checks(run.files)
    checks.results.setdefault("config load", "ok")
    if run.request.harness is Harness.CODEX:
        checks.version = "version unknown"
    prepared = None
    agent = None
    rc = 1
    try:
        if not run.files.path("request.json").exists():
            run.files.receive(run.request)
        run.files.write("phase", "check")
        run.files.write("prompt", run_host.render.brief(run.request.kind, text))
        inputs = prepare(run, checks)
        ready, value = inputs.ready, inputs.policy
        delivered: Outcome[object] = inputs.delivery
        prepared = ready.get() if ready.values else None
        if prepared is not None and prepared.delivery.signing is not None:
            key = prepared.delivery.signing
            signed = checks.check(
                "credential delivery",
                lambda: credentials.signing_agent(key, run.tmpdir, run.deadline),
            )
            delivered = signed
            agent = signed.get() if signed.values else None
        if prepared is not None:
            value = checks.check(
                "policy",
                lambda: run_host.checked_policy(run, prepared.paths, checks),
            )
        if value.values and run.request.harness is Harness.CODEX:
            checks.version = version(value.get()[1], run)
        command = checks.check(
            "harness command",
            lambda: harness_command(run, ready, value, delivered, inputs.audit),
        )
        checks.check(
            "sandbox",
            lambda: sandbox(run, inputs, value, command),
        )
        rc = checks.write()
    except run_host.RunInterruptedError, EOFError:
        rc = 143
    except OSError, ValueError, subprocess.SubprocessError:
        message = "run preparation failed at run files"
        raise Invalid(message) from None
    finally:
        stack.enter_context(run_host.process_group.ignore_signals())
        rc = run_host.finish(run, prepared, agent, rc)
    return rc


@dataclasses.dataclass(frozen=True)
class Preconditions:
    ready: Outcome[run_host.Prepared]
    policy: Outcome[tuple[run_host.Policy, str]]
    delivery: Outcome[credentials.Delivery]
    host: Outcome[None]
    proxy: Outcome[None]
    audit: Outcome[None]


def prepare(run: run_host.Run, checks: Checks) -> Preconditions:
    host = checks.check("host and account", lambda: host_and_account(run))
    paths = run_host.planned_paths(run)
    value = checks.check("policy", lambda: run_host.checked_policy(run, paths, checks))
    for name in PROTECTIONS:
        if name not in checks.results:
            if checks.results["policy"] == "ok":
                checks.results[name] = "ok"
            else:
                checks.skip(name, "policy")
    proxy = checks.check(
        "network proxy", lambda: run_host.check_proxy(run, value.get()[1])
    )
    workspace = checks.check(
        "mirror and clone", lambda: with_policy(value, lambda: create_workspace(run))
    )
    actual = checks.check(
        "workspace",
        lambda: dataclasses.replace(
            paths,
            worktree=str(workspace.get()),
            git_common_dir=str(workspace.get() / ".git"),
        ),
    )
    checks.check(".bazel_env", lambda: hook_tools(actual.get(), checks))
    delivery = checks.check(
        "credential delivery",
        lambda: with_policy(value, lambda: deliver(run)),
    )
    audit = checks.check(
        "audit directory",
        lambda: with_policy(value, lambda: audit_directory(run, paths)),
    )
    before = checks.check(
        "codex auth copy",
        lambda: with_policy(value, lambda: copy_auth(run, Path(paths.codex_home))),
    )
    return Preconditions(
        assemble(actual, delivery, before), value, delivery, host, proxy, audit
    )


def with_policy[T](
    value: Outcome[tuple[run_host.Policy, str]], operation: Callable[[], T]
) -> T:
    value.get()
    return operation()


def harness_command(
    run: run_host.Run,
    ready: Outcome[run_host.Prepared],
    value: Outcome[tuple[run_host.Policy, str]],
    delivered: Outcome[object],
    audit: Outcome[None],
) -> tuple[str, ...]:
    prepared = ready.get()
    delivered.get()
    audit.get()
    return run_host.checked_harness_command(run, prepared, *value.get())


def sandbox(
    run: run_host.Run,
    inputs: Preconditions,
    value: Outcome[tuple[run_host.Policy, str]],
    command: Outcome[tuple[str, ...]],
) -> None:
    invocation = command.get()
    inputs.host.get()
    inputs.proxy.get()
    run_host.check_sandbox(run, inputs.ready.get(), value.get()[0], invocation)


def assemble(
    actual: Outcome[run_host.RunPaths],
    delivery: Outcome[credentials.Delivery],
    before: Outcome[str | None],
) -> Outcome[run_host.Prepared]:
    try:
        paths = actual.get()
        delivered = delivery.get()
        auth = before.get()
    except NeedsError as exc:
        return Outcome(str(exc))
    paths = dataclasses.replace(
        paths,
        signing_socket=str(Path(paths.tmpdir) / "ssh.sock")
        if delivered.signing is not None
        else None,
    )
    return Outcome(actual.name, (run_host.Prepared(paths, delivered, auth),))


def create_workspace(run: run_host.Run) -> Path:
    result = run_host.process_group.prepare(
        lambda: run_host.worktree.create(
            Path(run_host.repo.clone_path(run.home, run.request.repo)),
            run.files.name,
            run.request,
            run.settings.public_repos,
        ),
        run.files,
        run.deadline,
        run.settings.kill_grace_seconds,
    )
    if not isinstance(result, Path):
        message = "run preparation returned no workspace"
        raise Invalid(message)
    return result


def deliver(run: run_host.Run) -> credentials.Delivery:
    result = run_host.process_group.prepare(
        lambda: credentials.deliver(run.request, run.settings, run.tmpdir),
        run.files,
        run.deadline,
        run.settings.kill_grace_seconds,
    )
    if not isinstance(result, credentials.Delivery):
        message = "run preparation failed at credential delivery"
        raise Invalid(message)
    return result


def audit_directory(run: run_host.Run, paths: run_host.RunPaths) -> None:
    run_host.process_group.prepare(
        lambda: runfiles.private_dir(Path(paths.audit_dir)),
        run.files,
        run.deadline,
        run.settings.kill_grace_seconds,
    )


def copy_auth(run: run_host.Run, destination: Path) -> str | None:
    if run.request.harness is not Harness.CODEX:
        return None

    def copy() -> str | None:
        runfiles.private_dir(destination)
        source = runfiles.private_dir(run.home / ".codex") / "auth.json"
        return credentials.auth_copy(source, destination / "auth.json")

    result = run_host.process_group.prepare(
        copy,
        run.files,
        run.deadline,
        run.settings.kill_grace_seconds,
    )
    if result is not None and not isinstance(result, str):
        message = "run preparation failed at codex auth copy"
        raise Invalid(message)
    return result


def hook_tools(paths: run_host.RunPaths, checks: Checks) -> None:
    checks.hook_tools = (
        "present"
        if (Path(paths.worktree) / ".bazel_env" / "bin").is_dir()
        else "missing; commit hooks that call .bazel_env/bin will fail in this run"
    )


def host_and_account(run: run_host.Run) -> None:
    run_host.check_host(run)
    run_host.check_account(run)
    if run.request.unsandboxed:
        run_host.mark_host(run)

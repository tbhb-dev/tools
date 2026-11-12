"""Push only disposable branches, with synthetic credentials in a pipe."""

from __future__ import annotations

import os
import select
import subprocess
from contextlib import ExitStack, contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast

import pytest
from agent_host_example import constant, host_settings, run_request

from tbhbdev.alpha.agent import credentials, host_tools, push, run_host, runfiles

if TYPE_CHECKING:
    from collections.abc import Callable, Generator

SYNTHETIC = "distinctive-synthetic-push-value-7391"


@pytest.fixture(autouse=True)
def push_account(
    isolated_agent_home: None, agent_suffix_case: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    del isolated_agent_home, agent_suffix_case
    original_read = runfiles.read_owned

    def read(source: Path, limit: int, label: str) -> str:
        for part in source.parts:
            if part.lower() == ".git":
                assert part == ".git"
        if source.name.lower() in ("head", "packed-refs"):
            assert source.name in ("HEAD", "packed-refs")
        expected = {
            "private-repos.txt": (1024 * 1024, "private-name list"),
            "HEAD": (1024, "run HEAD"),
            "packed-refs": (1024 * 1024, "packed refs"),
        }
        assert (limit, label) == expected.get(source.name, (1024, "run ref"))
        return original_read(source, limit, label)

    monkeypatch.setattr(runfiles, "read_owned", read)


@pytest.fixture
def local_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[run_host.Run, run_host.Prepared, Path]:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = tmp_path / "target.git"
    git(workspace, "init", "--initial-branch=agent/example")
    git(workspace, "init", "--bare", str(target))
    git(workspace, "config", "user.name", "Example")
    git(workspace, "config", "user.email", "example@example.com")
    git(workspace, "config", "commit.gpgsign", "false")
    commit(workspace, "initial")
    paths = run_host.RunPaths(
        str(tmp_path),
        str(workspace),
        str(tmp_path / "tmp"),
        str(tmp_path / "codex"),
        str(tmp_path / "audit"),
    )
    settings = host_settings()
    run = run_host.Run(
        runfiles.RunFiles(tmp_path / "state", "example"),
        run_request(),
        settings,
        settings.hosts[0],
        tmp_path,
        tmp_path / "tmp",
        0,
        100,
    )
    names = run.home / ".local/state/tbhb/private-repos.txt"
    names.parent.mkdir(parents=True)
    names.write_text("# names\n\n  fixture" + "repo  \n")
    prepared = run_host.Prepared(
        paths,
        credentials.Delivery({}, None, gh_fallback=False, claude_profile=None),
        None,
    )

    def destination(repository: str) -> str:
        assert repository == run.request.repo
        return target.as_uri()

    monkeypatch.setattr(push, "url", destination)
    return run, prepared, target


def git(directory: Path, *args: str) -> str:
    return subprocess.check_output(
        ["git", "-c", "core.hooksPath=/dev/null", "-C", str(directory), *args],
        cwd=directory,
        env={
            "PATH": os.environ["PATH"],
            "HOME": str(Path.home()),
            "XDG_CONFIG_HOME": str(Path.home() / ".config"),
            "XDG_STATE_HOME": str(Path.home() / ".local/state"),
            "XDG_CACHE_HOME": str(Path.home() / ".cache"),
            "XDG_DATA_HOME": str(Path.home() / ".local/share"),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
        },
        text=True,
        stderr=subprocess.DEVNULL,
    ).strip()


def commit(directory: Path, text: str) -> None:
    git(directory, "commit", "--allow-empty", "-qm", text)


def harness(run: run_host.Run, prepared: run_host.Prepared, stack: ExitStack) -> int:
    assert isinstance(stack, ExitStack)
    commit(Path(prepared.paths.worktree), "agent change")
    run.files.write("out.final", "report")
    return 0


def test_push_success(
    local_run: tuple[run_host.Run, run_host.Prepared, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run, prepared, target = local_run
    original = push.send

    def send(
        directory: Path, destination: str, branch: str, token: str
    ) -> tuple[int, str]:
        assert directory.parent == run.files.root
        assert destination == target.as_uri()
        assert branch == "agent/example"
        assert token == SYNTHETIC
        return original(directory, destination, branch, token)

    monkeypatch.setattr(push, "send", send)
    with ExitStack() as stack:
        assert push.run(run, prepared, stack, harness, SYNTHETIC) == 0
    head = git(target, "rev-parse", "refs/heads/agent/example")
    assert run.files.path("head").read_text() == head
    assert run.files.path("branch").read_text() == "agent/example"


@pytest.mark.parametrize(
    "rule", ["argv", "environment", "config", "write_roots", "output"]
)
def test_token_rules(
    rule: str,
    local_run: tuple[run_host.Run, run_host.Prepared, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run, prepared, _target = local_run
    original = cast("Callable[..., subprocess.Popen[str]]", subprocess.Popen)
    commands: list[str] = []
    environments: list[str] = []

    def spawn(*args: object, **kwargs: object) -> subprocess.Popen[str]:
        commands.append(str(args))
        environments.append(str(kwargs["env"]))
        return original(*args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", spawn)
    with ExitStack() as stack:
        assert push.run(run, prepared, stack, harness, SYNTHETIC) == 0
    match rule:
        case "argv":
            values = commands
        case "environment":
            values = environments
        case "config":
            values = [p.read_text() for p in run.home.rglob("config") if p.is_file()]
        case "write_roots":
            values = [
                p.read_bytes().decode(errors="replace")
                for p in run.home.rglob("*")
                if p.is_file()
            ]
        case _:
            values = [p.read_text() for p in run.files.root.iterdir() if p.is_file()]
    assert values
    assert all(SYNTHETIC not in value for value in values)


def test_skip(local_run: tuple[run_host.Run, run_host.Prepared, Path]) -> None:
    run, prepared, target = local_run
    with ExitStack() as stack:
        assert push.run(run, prepared, stack, harness, None) == 0
    assert git(target, "for-each-ref") == ""
    assert not run.files.path("branch").exists()


@pytest.mark.parametrize("rc", [0, 9])
def test_no_commit(
    local_run: tuple[run_host.Run, run_host.Prepared, Path], rc: int
) -> None:
    run, prepared, target = local_run
    with ExitStack() as stack:
        assert push.run(run, prepared, stack, constant(rc), SYNTHETIC) == rc
    assert git(target, "for-each-ref") == ""
    assert not run.files.path("head").exists()
    assert not run.files.path("log").exists()


def test_failure(
    local_run: tuple[run_host.Run, run_host.Prepared, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run, prepared, _target = local_run
    monkeypatch.setattr(push, "url", constant((run.home / "missing").as_uri()))
    with ExitStack() as stack:
        assert push.run(run, prepared, stack, harness, SYNTHETIC) == 4
    assert run.files.path("log").read_text().startswith("push failed: ")
    assert not run.files.path("branch").exists()


def test_helper_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    read_fd, write_fd = os.pipe()
    os.write(write_fd, (SYNTHETIC + "\n").encode())
    os.close(write_fd)
    try:
        command = push.helper(read_fd).removeprefix("!")
        for rc in (0, 1):
            child = subprocess.run(
                ["/bin/sh", "-c", command + " get"],
                input="protocol=https\nhost=github.com\n\n",
                text=True,
                capture_output=True,
                pass_fds=(read_fd,),
                cwd=tmp_path,
                check=False,
            )
            assert child.returncode == rc
            assert child.stdout == (
                f"username=x-access-token\npassword={SYNTHETIC}\n\n" if rc == 0 else ""
            )
    finally:
        os.close(read_fd)


def test_github_url() -> None:
    assert push.url("example/repo") == "https://github.com/example/repo.git"
    with pytest.raises(ValueError, match=r"^push requires github\.com$"):
        push.url("example.org/example/repo")


def test_broken_credential_pipe() -> None:
    read_fd, write_fd = os.pipe()
    os.close(read_fd)
    push.feed(write_fd, SYNTHETIC)
    with pytest.raises(OSError, match="Bad file descriptor"):
        os.fstat(write_fd)


@pytest.mark.parametrize("condition", ["uncertain", "wrong-branch"])
def test_do_not_push_other_branches(
    condition: str, local_run: tuple[run_host.Run, run_host.Prepared, Path]
) -> None:
    run, prepared, target = local_run
    if condition == "uncertain":
        run.files.uncertain = True
    else:
        git(Path(prepared.paths.worktree), "checkout", "-b", "other")
    with ExitStack() as stack:
        assert push.run(run, prepared, stack, harness, SYNTHETIC) == 0
    assert git(target, "for-each-ref") == ""


@pytest.mark.parametrize("error", ["", "fatal: " + SYNTHETIC + "\nsecond line"])
def test_failure_redacted(
    error: str,
    local_run: tuple[run_host.Run, run_host.Prepared, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run, prepared, _target = local_run
    monkeypatch.setattr(push, "send", constant((1, error)))
    with ExitStack() as stack:
        assert push.run(run, prepared, stack, harness, SYNTHETIC) == 4
    assert run.files.path("log").read_text() == (
        "push failed: fatal: [redacted]\n"
        if error
        else "push failed: git push failed\n"
    )


def test_spawn_failure_closes_pipe(
    local_run: tuple[run_host.Run, run_host.Prepared, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _run, prepared, _target = local_run
    created: list[int] = []
    original = os.pipe

    def pipe() -> tuple[int, int]:
        descriptors = original()
        created.extend(descriptors)
        return descriptors

    def fail(*_args: object, **_kwargs: object) -> None:
        message = "spawn failed"
        raise OSError(message)

    monkeypatch.setattr(os, "pipe", pipe)
    monkeypatch.setattr(subprocess, "Popen", fail)
    with pytest.raises(OSError, match="spawn failed"):
        push.send(Path(prepared.paths.worktree), "unused", "agent/example", SYNTHETIC)
    for descriptor in created:
        with pytest.raises(OSError, match="Bad file descriptor"):
            os.fstat(descriptor)


@pytest.mark.parametrize(
    ("operation", "query"),
    [
        ("store", "protocol=https\nhost=github.com\n\n"),
        ("erase", "protocol=https\nhost=github.com\n\n"),
        ("get", "protocol=http\nhost=github.com\n\n"),
        ("get", "protocol=https\nhost=example.org\n\n"),
        ("get", "protocol=https\n\n"),
        ("get", "host=github.com\n\n"),
        ("get", " protocol=https\nhost=github.com\n\n"),
        ("get", "protocol=https\nhost=github.com \n\n"),
        ("get", ""),
    ],
)
def test_helper_refuses_other_destinations(
    operation: str, query: str, tmp_path: Path
) -> None:
    read_fd, write_fd = os.pipe()
    os.write(write_fd, (SYNTHETIC + "\n").encode())
    os.close(write_fd)
    try:
        command = push.helper(read_fd).removeprefix("!")
        child = subprocess.run(
            ["/bin/sh", "-c", command + " " + operation],
            input=query,
            text=True,
            capture_output=True,
            pass_fds=(read_fd,),
            cwd=tmp_path,
            check=False,
        )
        assert child.returncode == 1
        assert child.stdout == ""
        assert child.stderr == ""
        assert os.read(read_fd, 4096) == (SYNTHETIC + "\n").encode()
    finally:
        os.close(read_fd)


def test_host_push_ignores_sandbox_git_config(
    local_run: tuple[run_host.Run, run_host.Prepared, Path],
) -> None:
    run, prepared, target = local_run
    workspace = Path(prepared.paths.worktree)
    hook = workspace / "pre-push"
    marker = workspace / "hook-ran"
    hook.write_text(f'#!/bin/sh\ntouch "{marker}"\nexit 1\n')
    hook.chmod(0o700)

    def hostile_harness(
        actual: run_host.Run, paths: run_host.Prepared, stack: ExitStack
    ) -> int:
        rc = harness(actual, paths, stack)
        git(workspace, "config", "core.hooksPath", str(workspace))
        git(
            workspace,
            "config",
            f"url.{(run.home / 'missing').as_uri()}.insteadOf",
            target.as_uri(),
        )
        return rc

    with ExitStack() as stack:
        assert push.run(run, prepared, stack, hostile_harness, SYNTHETIC) == 0
    assert not marker.exists()
    assert (
        git(target, "rev-parse", "refs/heads/agent/example")
        == run.files.path("head").read_text()
    )


@pytest.mark.parametrize("packed", [False, True])
@pytest.mark.parametrize("valid", [False, True])
def test_head_reads_only_owned_ref_data(
    packed: bool, valid: bool, local_run: tuple[run_host.Run, run_host.Prepared, Path]
) -> None:
    _run, prepared, _target = local_run
    workspace = Path(prepared.paths.worktree)
    expected = git(workspace, "rev-parse", "HEAD")
    if packed:
        git(workspace, "pack-refs", "--all")
        if not valid:
            (workspace / ".git" / "packed-refs").write_text(
                "# pack-refs\n" + expected + " refs/heads/other\n"
            )
    elif not valid:
        (workspace / ".git" / "refs/heads/agent/example").write_text("bad\n")
    if valid:
        assert push.head(workspace, "agent/example") == expected
    else:
        message = "missing run commit" if packed else "invalid run commit"
        with pytest.raises(ValueError, match=rf"^{message}$"):
            push.head(workspace, "agent/example")


@pytest.mark.parametrize("padding", [" ", "\t"])
def test_packed_ref_rejects_leading_whitespace(
    local_run: tuple[run_host.Run, run_host.Prepared, Path], padding: str
) -> None:
    _run, prepared, _target = local_run
    workspace = Path(prepared.paths.worktree)
    expected = git(workspace, "rev-parse", "HEAD")
    git(workspace, "pack-refs", "--all")
    (workspace / ".git" / "packed-refs").write_text(
        f"{padding}{expected} refs/heads/agent/example\n"
    )
    with pytest.raises(ValueError, match=r"^invalid run commit$"):
        push.head(workspace, "agent/example")


def test_detached_head_skips_push(
    local_run: tuple[run_host.Run, run_host.Prepared, Path],
) -> None:
    run, prepared, target = local_run
    git(Path(prepared.paths.worktree), "checkout", "--detach")
    with ExitStack() as stack:
        assert push.run(run, prepared, stack, harness, SYNTHETIC) == 0
    assert git(target, "for-each-ref") == ""


def test_large_token_does_not_block_unused_helper(
    local_run: tuple[run_host.Run, run_host.Prepared, Path],
) -> None:
    run, prepared, _target = local_run
    with ExitStack() as stack:
        assert push.run(run, prepared, stack, harness, SYNTHETIC * 10000) == 0


def test_push_start_failure_is_recorded(
    local_run: tuple[run_host.Run, run_host.Prepared, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run, prepared, _target = local_run

    def fail(*_args: object) -> None:
        raise OSError

    monkeypatch.setattr(push, "send", fail)
    with ExitStack() as stack:
        assert push.run(run, prepared, stack, harness, SYNTHETIC) == 4
    assert run.files.path("log").read_text() == "push failed: git push unavailable\n"


def test_send_limits_child_capabilities(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: list[dict[str, object]] = []

    @contextmanager
    def child(command: list[str], **kwargs: object) -> Generator[SimpleNamespace]:
        captured.append({"command": command, **kwargs})
        descriptors = cast("tuple[int, ...]", kwargs["pass_fds"])
        duplicate = os.dup(descriptors[0])

        def communicate() -> tuple[None, str]:
            assert select.select([duplicate], [], [], 5)[0] == [duplicate]
            assert os.read(duplicate, 4096) == (SYNTHETIC + "\n").encode()
            return None, "one error\n"

        try:
            yield SimpleNamespace(returncode=4, communicate=communicate)
        finally:
            os.close(duplicate)

    monkeypatch.setattr(subprocess, "Popen", child)
    assert push.send(
        tmp_path, "https://github.com/example/repo.git", "agent/example", SYNTHETIC
    ) == (4, "one error\n")
    assert len(captured) == 1
    spec = captured[0]
    descriptors = cast("tuple[int, ...]", spec["pass_fds"])
    assert len(descriptors) == 1
    command = cast("list[str]", spec.pop("command"))
    assert command[0] == host_tools.resolve("git")
    assert command[-6:] == [
        "-C",
        str(tmp_path),
        "push",
        "--",
        "https://github.com/example/repo.git",
        "HEAD:refs/heads/agent/example",
    ]
    options = command[1:-6]
    assert options[::2] == ["-c"] * 6
    assert options[1::2][:2] == [
        "credential.helper=",
        f"credential.helper={push.helper(descriptors[0])}",
    ]
    assert set(options[1::2][2:]) == {
        "core.askPass=",
        "core.fsmonitor=false",
        "core.hooksPath=/dev/null",
        "credential.useHttpPath=false",
    }
    environment = cast("dict[str, str]", spec.pop("env"))
    assert environment == {
        "GIT_ASKPASS": "/bin/false",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_TERMINAL_PROMPT": "0",
        "HOME": str(tmp_path),
        "PATH": host_tools.search(),
        "XDG_CACHE_HOME": str(tmp_path),
        "XDG_CONFIG_HOME": str(tmp_path),
        "XDG_DATA_HOME": str(tmp_path),
        "XDG_STATE_HOME": str(tmp_path),
    }
    assert spec == {
        "cwd": tmp_path,
        "errors": "replace",
        "pass_fds": descriptors,
        "stderr": subprocess.PIPE,
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "text": True,
    }
    with pytest.raises(OSError, match="Bad file descriptor"):
        os.fstat(descriptors[0])


def test_push_preserves_harness_failure(
    local_run: tuple[run_host.Run, run_host.Prepared, Path],
) -> None:
    run, prepared, target = local_run

    def failed_harness(
        actual: run_host.Run, paths: run_host.Prepared, stack: ExitStack
    ) -> int:
        harness(actual, paths, stack)
        return 7

    with ExitStack() as stack:
        assert push.run(run, prepared, stack, failed_harness, SYNTHETIC) == 7
    assert git(target, "for-each-ref") == ""
    assert run.files.path("head").exists()
    assert run.files.path("log").read_text() == "push skipped: harness exited 7\n"
    assert not run.files.path("branch").exists()


def test_feed_sends_one_line_and_closes() -> None:
    read_fd, write_fd = os.pipe()
    try:
        push.feed(write_fd, SYNTHETIC)
        assert os.read(read_fd, 4096) == (SYNTHETIC + "\n").encode()
        assert os.read(read_fd, 4096) == b""
    finally:
        os.close(read_fd)


@pytest.mark.parametrize("contents", [None, "", " \n# names\n  # more names\n"])
def test_private_name_list_required(
    contents: str | None,
    local_run: tuple[run_host.Run, run_host.Prepared, Path],
) -> None:
    run, prepared, target = local_run
    source = run.home / ".local/state/tbhb/private-repos.txt"
    if contents is None:
        source.rename(source.with_suffix(".saved"))
    else:
        source.write_text(contents)
    with ExitStack() as stack:
        assert push.run(run, prepared, stack, harness, SYNTHETIC) == 4
    assert git(target, "for-each-ref") == ""
    assert run.files.path("log").read_text() == (
        f"push refused: no private-name list at {source}\n"
    )
    assert run.files.path("head").exists()
    assert not run.files.path("branch").exists()


@pytest.mark.parametrize(
    "position",
    ["message", "addition", "path", "uppercase-path", "git-path", "configured"],
)
def test_private_name_refuses_push(
    position: str,
    local_run: tuple[run_host.Run, run_host.Prepared, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run, prepared, target = local_run
    name = "fixture" + "repo"
    path = "tbhb-dev/op" + "s"
    if position == "uppercase-path":
        path = path.upper()
    elif position == "git-path":
        path = "op" + "s.git"
    is_path = position.endswith("path")
    if position == "configured":
        monkeypatch.setattr(push, "_configured", constant((name,)))
        (run.home / push.PRIVATE_NAMES).write_text("other" + "fixture\n")

    def named_harness(
        actual: run_host.Run, paths: run_host.Prepared, stack: ExitStack
    ) -> int:
        workspace = Path(paths.paths.worktree)
        if position in ("message", "configured"):
            commit(workspace, name)
        else:
            source = workspace / (path if is_path else "example.txt")
            source.parent.mkdir(parents=True, exist_ok=True)
            source.write_text(name if position == "addition" else "clean\n")
            git(workspace, "add", ".")
            commit(workspace, "change")
        assert isinstance(actual, run_host.Run)
        assert isinstance(stack, ExitStack)
        return 0

    with ExitStack() as stack:
        assert push.run(run, prepared, stack, named_harness, SYNTHETIC) == 4
    assert git(target, "for-each-ref") == ""
    log = run.files.path("log").read_text()
    assert log.startswith("push refused: ")
    assert " private-name hit(s)\nline " in log
    assert (f"path to private repo: {path}" if is_path else name) in log
    assert not run.files.path("branch").exists()


def test_scan_isolates_git_and_checks_only_own_range(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def log(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        assert command[:1] == [host_tools.resolve("git")]
        assert command[1:9] == [
            part
            for option in (
                "core.hooksPath=/dev/null",
                "core.fsmonitor=false",
                "diff.external=",
                "core.pager=cat",
            )
            for part in ("-c", option)
        ]
        assert command[9:12] == ["-C", str(tmp_path), "log"]
        assert command[12:] == [
            "--format=commit %H%n%B",
            "-p",
            "--no-ext-diff",
            "--no-text" + "conv",
            "base..head",
        ]
        assert kwargs == {
            "cwd": tmp_path,
            "env": {
                "HOME": str(tmp_path),
                "XDG_CACHE_HOME": str(tmp_path),
                "XDG_CONFIG_HOME": str(tmp_path),
                "XDG_DATA_HOME": str(tmp_path),
                "XDG_STATE_HOME": str(tmp_path),
                "GIT_ASKPASS": "/bin/false",
                "PATH": host_tools.search(),
                "GIT_CONFIG_GLOBAL": "/dev/null",
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_TERMINAL_PROMPT": "0",
            },
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.DEVNULL,
            "text": True,
            "errors": "replace",
            "check": True,
        }
        return subprocess.CompletedProcess(command, 0, "clean\n")

    monkeypatch.setattr(subprocess, "run", log)
    push.scan(tmp_path, "base", "head", ["fixture" + "repo"])


def test_scan_reports_every_hit_with_its_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    name = "fixture" + "repo"
    path = "tbhb-dev/op" + "s"
    monkeypatch.setattr(
        subprocess,
        "run",
        constant(subprocess.CompletedProcess([], 0, f"clean\n{path}\n{name}\n")),
    )
    monkeypatch.setattr(push, "_configured", constant([]))
    with pytest.raises(push.PushRefusedError) as caught:
        push.scan(tmp_path, "base", "head", [name])
    assert str(caught.value) == (
        f"2 private-name hit(s)\nline 3: '{name}' names a private repository; "
        f"describe it generically instead\nline 2: path to private repo: {path}"
    )


def test_no_base_commit_refuses_push(
    local_run: tuple[run_host.Run, run_host.Prepared, Path],
) -> None:
    run, prepared, target = local_run
    workspace = Path(prepared.paths.worktree)
    git(workspace, "checkout", "--detach")

    def branch_harness(
        actual: run_host.Run, paths: run_host.Prepared, stack: ExitStack
    ) -> int:
        git(workspace, "checkout", "agent/example")
        return harness(actual, paths, stack)

    with ExitStack() as stack:
        assert push.run(run, prepared, stack, branch_harness, SYNTHETIC) == 4
    assert git(target, "for-each-ref") == ""
    assert run.files.path("log").read_text() == "push refused: no base commit\n"
    assert not run.files.path("branch").exists()


def test_git_log_failure_refuses_push(
    local_run: tuple[run_host.Run, run_host.Prepared, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run, prepared, target = local_run
    original = cast("Callable[..., subprocess.CompletedProcess[str]]", subprocess.run)

    def fail_log(
        command: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        if "log" in command:
            raise subprocess.CalledProcessError(1, command)
        return original(command, **kwargs)

    monkeypatch.setattr(subprocess, "run", fail_log)
    with ExitStack() as stack:
        assert push.run(run, prepared, stack, harness, SYNTHETIC) == 4
    assert git(target, "for-each-ref") == ""
    assert run.files.path("log").read_text() == "push refused: git log unavailable\n"
    assert not run.files.path("branch").exists()

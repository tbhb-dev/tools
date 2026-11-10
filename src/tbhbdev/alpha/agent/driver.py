"""The coordinating host: gather facts, ask rules, execute bounded actions."""

from __future__ import annotations

import json
import os
import time
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from tbhbdev import version
from tbhbdev.alpha.agent import render, transport
from tbhbdev.alpha.agent.decisions import driver as rules
from tbhbdev.alpha.agent.decisions import remote, request
from tbhbdev.alpha.agent.decisions import runfiles_names as files
from tbhbdev.alpha.agent.model import Harness, Invalid, Profile, RunKind, RunRequest

if TYPE_CHECKING:
    import argparse

    from tbhbdev.alpha.agent.model import Host, Settings


class Run:
    """The recorded target and request, with no settings or credential references."""

    def __init__(self, name: str, host: Host, run: RunRequest, started: str) -> None:
        self.name = name
        self.host = host
        self.request = run
        self.started = started


def directory() -> Path:
    return (
        Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state")
        / files.DIRECTORY
    )


def metadata_path(name: str) -> Path:
    rules.check_name(name)
    return directory() / f"{name}.json"


def save(run: Run) -> None:
    path = metadata_path(run.name)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    record = {
        **request.to_dict(run.request),
        "host": run.host.name,
        "started": run.started,
    }
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as output:
        json.dump(record, output)
        output.write("\n")


def load(settings: Settings, name: str) -> Run:
    path = metadata_path(name)
    path.parent.chmod(0o700)
    try:
        record: dict[str, object] = json.loads(path.read_text())
        host = rules.select_host(
            settings, str(record.pop("host")), str(record["account"])
        )
        started = str(record.pop("started"))
        run = request.parse(record, host_allows_unsandboxed=host.unsandboxed)
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        message = f"{path}: invalid run metadata"
        raise Invalid(message) from exc
    return Run(name, host, run, started)


def prepare(settings: Settings, args: argparse.Namespace) -> Run:
    rules.check_name(args.name)
    host = rules.select_host(settings, args.host, args.account)
    harness = Harness(args.harness)
    profile = Profile(args.profile)
    rules.check_model(harness, args.model, allow_non_gemini=args.allow_non_gemini)
    # A review's final commit is read remotely after all local checks pass.
    commit = "0" * 40 if profile is Profile.REVIEW else None
    run = RunRequest(
        kind=RunKind.EXEC,
        repo=args.repo,
        commit=commit,
        harness=harness,
        model=args.model,
        effort=args.effort,
        profile=profile,
        claude_profile=args.claude_profile,
        reason=args.reason,
        account=host.account,
        timeout=args.timeout
        if args.timeout is not None
        else (settings.agy_timeout_seconds if harness is Harness.AGY else None),
        class_=args.class_,
        variant=args.variant,
        role=args.role,
        unsandboxed=args.unsandboxed is not None,
        authorized_by=args.unsandboxed,
        startup_deadline_seconds=settings.launch_wait_seconds,
    )
    run = rules.check(
        run, settings, host, ref=args.ref, exists=metadata_path(args.name).exists()
    )
    return Run(args.name, host, run, datetime.now(UTC).isoformat())


def execute(settings: Settings, args: argparse.Namespace) -> int:
    run = prepare(settings, args)
    prompt = render.brief(Path(args.prompt_file).read_text())
    schema = None if args.schema is None else Path(args.schema).read_text()
    if schema is not None:
        json.loads(schema)
    deadline = time.monotonic() + settings.launch_wait_seconds
    ref = args.ref or "HEAD"
    resolve = (
        run.request.profile is Profile.REVIEW and rules.COMMIT.fullmatch(ref) is None
    )
    script = (
        remote.preflight(run.request.repo, ref, version.line(), run.host)
        if resolve
        else remote.installed(run.host)
    )
    preflight = transport.shell(
        settings, run.host, script, timeout=deadline - time.monotonic()
    )
    host_version, _, refs = preflight.partition("\n")
    rules.same_version(version.line(), host_version)
    if run.request.profile is Profile.REVIEW:
        run.request = replace(run.request, commit=rules.resolve_ref(ref, refs))
    save(run)
    try:
        transport.stream(
            settings,
            run.host,
            run.name,
            inputs=transport.Inputs(
                json.dumps(request.to_dict(run.request)), prompt, schema
            ),
            timeout=deadline - time.monotonic(),
        )
        transport.call(
            settings,
            run.host,
            remote.start(run.name, settings.sockets_dir, run.host),
            timeout=deadline - time.monotonic(),
        )
    except transport.CollisionError:
        archive_metadata(run.name, run.started)
        raise
    except transport.TransportError:
        print(f"{run.name}: launch unconfirmed host={run.host.name} phase=unreachable")
        return 1
    try:
        return poll_launch(settings, run, deadline)
    except transport.TransportError:
        return unconfirmed(settings, run, "unreachable")


def poll_launch(settings: Settings, run: Run, deadline: float) -> int:
    while True:
        reading = transport.read(
            settings, run.host, run.name, timeout=deadline - time.monotonic()
        )
        action = rules.launch(
            reading.rc, launched=reading.launched, expired=time.monotonic() >= deadline
        )
        if action == "finished":
            # The host may still be closing its pane after committing the rc.
            transport.shell(
                settings,
                run.host,
                remote.session(run.name, "kill-session", settings.sockets_dir),
            )
            print(f"{run.name}: rc={reading.rc}\n{reading.text}", end="")
            return reading.rc or 0
        if action == "launched":
            print(render.launched(run.name, run.host.name, run.host.account))
            return 0
        if action == "deadline":
            return unconfirmed(settings, run, reading.phase)
        time.sleep(0.5)


def unconfirmed(settings: Settings, run: Run, phase: str) -> int:
    print(f"{run.name}: launch unconfirmed host={run.host.name} phase={phase}")
    kill(settings, run)
    return 1


def wait(settings: Settings, run: Run) -> int:
    rules.wait_kind(run.request.kind)
    poll = rules.wait_seconds(
        os.environ.get("TBHB_AGENT_WAIT_POLL", "30"), "TBHB_AGENT_WAIT_POLL"
    )
    unreachable = rules.wait_seconds(
        os.environ.get("TBHB_AGENT_WAIT_UNREACHABLE", "240"),
        "TBHB_AGENT_WAIT_UNREACHABLE",
    )
    failed_since = None
    while True:
        attempt = time.monotonic()
        timeout = (
            unreachable
            if failed_since is None
            else unreachable - (attempt - failed_since)
        )
        try:
            reading = transport.read(
                settings, run.host, run.name, answer=True, timeout=min(30, timeout)
            )
        except transport.TransportError:
            now = time.monotonic()
            if failed_since is None:
                failed_since = attempt
            result = rules.wait(
                None,
                session=True,
                failed_seconds=now - failed_since,
                unreachable=unreachable,
            )
            if result is not None:
                print(f"{run.name}: rc=unreachable")
                return result
        else:
            failed_since = None
            result = rules.wait(
                reading.rc,
                session=reading.session,
                failed_seconds=0,
                unreachable=unreachable,
            )
            if result is not None:
                rc = rules.answer_state(reading.rc)
                print(f"{run.name}: rc={rc}\n{reading.text}", end="")
                return result
        time.sleep(max(0.5, poll))


def status(settings: Settings, run: Run) -> int:
    reading = transport.read(settings, run.host, run.name)
    print(
        render.status(
            run.name,
            phase=reading.phase,
            rc=rules.state(reading.rc, session=reading.session),
            target=(run.host.name, run.host.account),
            started=run.started,
        )
    )
    return 0


def kill(settings: Settings, run: Run) -> int:
    transport.shell(
        settings,
        run.host,
        remote.session(run.name, "kill-session", settings.sockets_dir),
    )
    deadline = time.monotonic() + settings.kill_grace_seconds + 2
    while True:
        remaining = deadline - time.monotonic()
        reading = transport.read(
            settings, run.host, run.name, timeout=min(30, max(1, remaining))
        )
        action = rules.kill(
            reading.rc, session=reading.session, expired=time.monotonic() >= deadline
        )
        if action == "finished":
            return 0
        if action == "write":
            transport.shell(
                settings, run.host, remote.fallback(run.name, settings.sockets_dir)
            )
            return 0
        if action == "refuse":
            message = f"{run.name}: session still exists; return code left to run-host"
            raise Invalid(message)
        time.sleep(0.5)


def archive_metadata(name: str, stamp: str) -> None:
    trash = directory() / "trash" / stamp
    trash.mkdir(mode=0o700, parents=True)
    metadata_path(name).rename(trash / f"{name}.json")


def clean(settings: Settings, run: Run, *, force: bool) -> int:
    reading = transport.read(settings, run.host, run.name)
    rules.clean(reading.rc, session=reading.session, force=force)
    count = transport.shell(
        settings, run.host, remote.branch_facts(run.request.repo, run.name)
    )
    delete = rules.delete_branch(unpushed=int(count) > 0, force=force)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    transport.shell(
        settings,
        run.host,
        remote.clean(
            run.request.repo,
            run.name,
            stamp,
            settings.sockets_dir,
            delete_branch=delete,
        ),
    )
    archive_metadata(run.name, stamp)
    return 0


def keep(settings: Settings, run: Run) -> int:
    transport.shell(settings, run.host, remote.keep(run.name))
    return 0


def dispatch(settings: Settings, args: argparse.Namespace) -> int:
    if args.verb == "exec":
        return execute(settings, args)
    run = load(settings, args.name)
    if args.verb == "clean":
        return clean(settings, run, force=args.force)
    verbs = {"wait": wait, "status": status, "kill": kill, "keep": keep}
    return verbs[args.verb](settings, run)

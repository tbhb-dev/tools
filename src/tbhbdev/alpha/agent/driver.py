"""The coordinating host: gather facts, ask rules, execute bounded actions."""

from __future__ import annotations

import json
import os
import secrets
import sys
import time
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, NoReturn

from tbhbdev import version
from tbhbdev.alpha.agent import render, runfiles, taint_store, transport
from tbhbdev.alpha.agent.decisions import driver as rules
from tbhbdev.alpha.agent.decisions import remote, repo, request, taint
from tbhbdev.alpha.agent.decisions import runfiles_names as files
from tbhbdev.alpha.agent.model import Harness, Invalid, Profile, RunKind, RunRequest

if TYPE_CHECKING:
    import argparse

    from tbhbdev.alpha.agent.model import Host, Settings


class Run:
    """The recorded target and request, with no settings or credential references."""

    def __init__(
        self, name: str, host: Host, run: RunRequest, started: str, owner: str
    ) -> None:
        self.name = name
        self.host = host
        self.request = run
        self.started = started
        self.owner = owner
        self.cleaning = False


def directory() -> Path:
    return runfiles.state_home(Path.home()) / files.DIRECTORY


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
        "owner": run.owner,
        "cleaning": run.cleaning,
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
        owner = str(record.pop("owner"))
        cleaning = record.pop("cleaning")
        # Launch permission applies to new runs; recorded runs remain manageable.
        run = request.parse(record, host_allows_unsandboxed=True)
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        message = f"{path}: invalid run metadata"
        raise Invalid(message) from exc
    result = Run(name, host, run, started, owner)
    result.cleaning = bool(cleaning)
    return result


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
        tainted_host=args.unsandboxed is not None
        or taint.request_flag(taint_store.load(host.name)),
        authorized_by=args.unsandboxed,
        startup_deadline_seconds=settings.launch_wait_seconds,
    )
    run = rules.check(
        run, settings, host, ref=args.ref, exists=metadata_path(args.name).exists()
    )
    return Run(
        args.name, host, run, datetime.now(UTC).isoformat(), secrets.token_hex(16)
    )


def launch_trust(settings: Settings, run: Run, deadline: float) -> int:
    """Consult coordinator evidence before contacting the run host."""
    state = taint_store.load(run.host.name)
    if state.status == "clean":
        try:
            marker = transport.shell(
                settings,
                run.host,
                remote.host_marker(run.request.account),
                timeout=deadline - time.monotonic(),
            )
        except transport.TransportError:
            decision = taint.unreadable_marker(run.host.name)
            print(decision.message, file=sys.stderr)
            return decision.status
        else:
            if marker:
                record = taint.Taint(
                    run.host.name,
                    run.host.account,
                    "host-marker",
                    "host-marker",
                    datetime.now(UTC).isoformat(),
                )
                taint_store.record_taint(record)
                state = taint.State(run.host.name, "tainted")
    decision = taint.check_launch(run.request.profile, tainted=state)
    if decision.status:
        print(decision.message, file=sys.stderr)
        return decision.status
    if run.request.unsandboxed:
        taint_store.record_taint(
            taint.Taint(
                run.host.name,
                run.host.account,
                run.name,
                str(run.request.authorized_by),
                run.started,
            )
        )
    run.request = replace(
        run.request,
        tainted_host=run.request.unsandboxed or taint.request_flag(state),
    )
    return 0


def clear_taint(host: str, reason: str) -> int:
    taint_store.record_clearance(
        taint.Clearance(host, reason, datetime.now(UTC).isoformat())
    )
    print(f"{host}: host={taint_store.load(host).status}")
    return 0


def execute(settings: Settings, args: argparse.Namespace) -> int:
    run = prepare(settings, args)
    prompt = Path(args.prompt_file).read_text()
    schema = None if args.schema is None else Path(args.schema).read_text()
    if schema is not None:
        json.loads(schema)
    deadline = time.monotonic() + settings.launch_wait_seconds
    trust_status = launch_trust(settings, run, deadline)
    if trust_status:
        return trust_status
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
                json.dumps(request.to_dict(run.request)), prompt, schema, run.owner
            ),
            timeout=deadline - time.monotonic(),
        )
        transport.call(
            settings,
            run.host,
            remote.start(run.name, settings.sockets_dir, run.host, owner=run.owner),
            timeout=deadline - time.monotonic(),
        )
    except transport.CollisionError:
        archive_metadata(run.name, run.started)
        raise
    except transport.TransportError:
        recover_transfer(settings, run)
        print(
            f"{run.name}: launch unconfirmed host={run.host.name} phase=unreachable"
            + render.sandbox(run.request.authorized_by)
            + render.trust(tainted_host=run.request.tainted_host)
        )
        return 75
    try:
        return poll_launch(settings, run, deadline)
    except transport.CollisionError:
        not_owner(run)
    except transport.TransportError:
        return unconfirmed(settings, run, "unreachable")


def not_owner(run: Run) -> NoReturn:
    archive_metadata(run.name, datetime.now(UTC).isoformat())
    message = f"{run.name}: not owner"
    raise Invalid(message)


def recover_transfer(settings: Settings, run: Run) -> None:
    try:
        transport.shell(settings, run.host, remote.owned(run.name, run.owner))
    except transport.CollisionError:
        not_owner(run)
    except transport.TransportError:
        # An unreachable host may have started the run. Keep its owner claim
        # so later verbs can reach it once the connection recovers.
        pass


def poll_launch(settings: Settings, run: Run, deadline: float) -> int:
    while True:
        reading = transport.read(
            settings,
            run.host,
            (run.name, run.owner),
            timeout=deadline - time.monotonic(),
        )
        action = rules.launch(
            reading.rc, launched=reading.launched, expired=time.monotonic() >= deadline
        )
        if action == "finished":
            # The host may still be closing its pane after committing the rc.
            transport.shell(
                settings,
                run.host,
                remote.session(
                    run.name, "kill-session", settings.sockets_dir, owner=run.owner
                ),
            )
            print(
                f"{run.name}: rc={reading.rc}"
                f"{render.sandbox(run.request.authorized_by)}"
                f"{render.trust(tainted_host=run.request.tainted_host)}\n{reading.text}",
                end="",
            )
            return reading.rc or 0
        if action == "launched":
            print(
                render.launched(
                    run.name,
                    run.host.name,
                    run.host.account,
                    authorized_by=run.request.authorized_by,
                    tainted_host=run.request.tainted_host,
                )
            )
            return 0
        if action == "deadline":
            return unconfirmed(settings, run, reading.phase)
        time.sleep(0.5)


def unconfirmed(settings: Settings, run: Run, phase: str) -> int:
    print(
        f"{run.name}: launch unconfirmed host={run.host.name} phase={phase}"
        + render.sandbox(run.request.authorized_by)
        + render.trust(tainted_host=run.request.tainted_host)
    )
    try:
        kill(settings, run)
    except transport.CollisionError:
        not_owner(run)
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
                settings,
                run.host,
                (run.name, run.owner),
                answer=True,
                timeout=min(30, timeout),
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
                print(
                    f"{run.name}: rc=unreachable"
                    + render.sandbox(run.request.authorized_by)
                )
                print(render.INSPECTION_WARNING)
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
                print(
                    f"{run.name}: rc={rc}"
                    f"{render.sandbox(run.request.authorized_by)}\n{reading.text}",
                    end="",
                )
                print("\n" + render.INSPECTION_WARNING)
                settle(settings, run, result)
                return result
        time.sleep(max(0.5, poll))


def settle(settings: Settings, run: Run, result: int) -> None:
    if result == 0:
        try:
            if clean(settings, run, force=False) == 0:
                return
        except (Invalid, transport.TransportError) as error:
            print(error, file=sys.stderr)
    tree = repo.clone_path(PurePosixPath("."), run.request.repo)
    print(
        f"{run.name}: worktree kept at "
        f"{run.host.name}:~/{tree}/.worktrees/agent-{run.name}"
    )


def status(settings: Settings, run: Run) -> int:
    state = taint_store.load(run.host.name)
    reading = transport.read(
        settings,
        run.host,
        (run.name, run.owner),
    )
    print(
        render.status(
            run.name,
            phase=reading.phase,
            rc=rules.state(reading.rc, session=reading.session),
            target=(run.host.name, run.host.account, run.request.authorized_by),
            started=run.started,
        )
        + (f" host={state.status}" if state.tainted else "")
    )
    return 0


def kill(settings: Settings, run: Run) -> int:
    transport.shell(
        settings,
        run.host,
        remote.session(run.name, "kill-session", settings.sockets_dir, owner=run.owner),
    )
    deadline = time.monotonic() + settings.kill_grace_seconds + 2
    while True:
        remaining = deadline - time.monotonic()
        reading = transport.read(
            settings,
            run.host,
            (run.name, run.owner),
            timeout=min(30, max(1, remaining)),
        )
        action = rules.kill(
            reading.rc, session=reading.session, expired=time.monotonic() >= deadline
        )
        if action == "finished":
            return 0
        if action == "write":
            transport.shell(
                settings,
                run.host,
                remote.fallback(run.name, settings.sockets_dir, owner=run.owner),
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
    if run.request.unsandboxed and not force:
        print("unsandboxed run: pass --force to move it to trash", file=sys.stderr)
        return 65
    reading = transport.read(
        settings,
        run.host,
        (run.name, run.owner),
    )
    rules.clean(reading.rc, session=reading.session, force=force or run.cleaning)
    if not force:
        branch = transport.shell(
            settings,
            run.host,
            remote.origin_branch(run.request.repo, run.name, owner=run.owner),
        )
        if not any(
            line.endswith(f"\trefs/heads/agent/{run.name}")
            for line in branch.splitlines()
        ):
            message = (
                f"branch agent/{run.name} does not exist on origin; "
                "pass --force to clean anyway"
            )
            raise Invalid(message)
        print("branch exists on origin")
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    record = json.loads(metadata_path(run.name).read_text())
    record["cleaning"] = True
    metadata_path(run.name).write_text(json.dumps(record) + "\n")
    transport.shell(
        settings,
        run.host,
        remote.clean(
            run.request.repo,
            run.name,
            stamp,
            settings.sockets_dir,
            decision=remote.Cleanup(run.owner, force),
        ),
    )
    archive_metadata(run.name, stamp)
    return 0


def inspect(settings: Settings, run: Run, *, limit: int | None) -> int:
    if run.request.unsandboxed:
        print(f"unsandboxed run: inspect agent/{run.name} on GitHub", file=sys.stderr)
        return 65
    print(
        transport.terminal_text(
            transport.shell(
                settings,
                run.host,
                remote.inspect(
                    run.request.repo, run.name, owner=run.owner, limit=limit
                ),
            )
        ),
        end="",
    )
    return 0


def keep(settings: Settings, run: Run) -> int:
    transport.shell(settings, run.host, remote.keep(run.name, owner=run.owner))
    return 0


def dispatch(settings: Settings, args: argparse.Namespace) -> int:
    if args.verb == "clear-taint":
        return clear_taint(args.host, args.reason)
    if args.verb == "exec":
        return execute(settings, args)
    run = load(settings, args.name)
    verbs = {"wait": wait, "status": status, "kill": kill, "keep": keep}
    try:
        if args.verb in {"log", "diff"}:
            return inspect(settings, run, limit=args.n if args.verb == "log" else None)
        if args.verb == "clean":
            return clean(settings, run, force=args.force)
        return verbs[args.verb](settings, run)
    except transport.CollisionError:
        not_owner(run)

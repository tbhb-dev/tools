"""Coordinator rules over gathered values; no I/O or host-side lifecycle rules."""

from __future__ import annotations

import math
import re
from dataclasses import replace
from typing import TYPE_CHECKING

from tbhbdev.alpha import brief
from tbhbdev.alpha.agent.decisions import config, request
from tbhbdev.alpha.agent.model import NAME, Harness, Invalid, Profile, RunKind

if TYPE_CHECKING:
    from collections.abc import Iterator

    from tbhbdev.alpha.agent.model import Host, RunRequest, Settings

COMMIT = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
_PROMPT_REF = re.compile(
    rf"{brief.REF_START}refs/(?:pull/[0-9]+/head|heads/{brief.BRANCH_REF}){brief.REF_END}"
)
_REF_USE = re.compile(
    r"\b(?:(?i:use|reuse|fetch|ref|start from|check out|switch to"
    r"|git (?:checkout|switch|show|merge|rebase)) "
    r"|(?i:git fetch) \S+ )[\[(`\"']?"
    rf"({brief.SHORT_AGENT_REF})"
)
_PUSH_TO = r"push (?:\w+ )?to"
_UPDATE = re.compile(
    rf"\b(?:(?i:update|{_PUSH_TO}) "
    r"(?:(?i:a|the|this|its|current|existing|branch) )*"
    r"|(?i:git push) [^\s`]+ (?:[^\s`:]+:)?)"
    rf"(?:[`\"']?(refs/heads/{brief.BRANCH_REF}{brief.REF_END}|{brief.SHORT_AGENT_REF})"
    r"|(?i:(?:PR|pull request)(?: #\d+)? (?:head|branch))\b)"
)
_REFUSED_COMMAND_EXAMPLE = re.compile(
    r"(?P<quote>[`\"'])(?i:git (?:checkout|switch|push)) "
    r"(?:(?!(?P=quote))[^\n])+(?P=quote)"
    r"(?=\s+(?i:(?:(?:must|should|will) be|is) (?:refused|forbidden|rejected))\b)"
)
_INSTRUCTION_START = r"(?:^|(?<=[.!?;]) +)(?:[-*+] |\d+[.)] )?(?:(?i:then) )?"
_BRANCH_NAME = rf"(?:refs/heads/)?(?!refs/|[-./]){brief.BRANCH_REF}"
_BRANCH_PHRASE = r"(?:a |the )?branch (?:named )?"
_GIT_OPTIONS = r"(?: --?\w[\w-]*)*"
# A plain checkout can also restore files. Require a single branch name.
_CHECKOUT_BRANCH = (
    rf"(?=(?:refs/heads/{brief.BRANCH_REF}|(?!HEAD{brief.REF_END})[\w/-]*[\w-])"
    r"(?=[`]|[\"']?(?: *[;&|]|[.,!?](?: |$)|$)))"
)
_BRANCH_COMMAND = re.compile(
    rf"{_INSTRUCTION_START}(?:(?i:run|use|execute)(?: |: *))?[`\"']?(?i:git) "
    rf"(?:(?i:checkout){_GIT_OPTIONS} -[bB] [`\"']?"
    rf"|(?i:checkout)(?: -q| --quiet)? [`\"']?{_CHECKOUT_BRANCH}"
    rf"|(?i:switch)(?!{_GIT_OPTIONS} (?:-d|--detach)\b)"
    rf"{_GIT_OPTIONS}(?: -[cC])?(?: --)? [`\"']?"
    rf"|(?i:push){_GIT_OPTIONS} (?!-)[^\s`\"']+ [`\"']?\+?"
    rf"(?:[^\s`\"':]+:)?(?!HEAD{brief.REF_END}))"
    rf"({_BRANCH_NAME})(?!:){brief.REF_END}",
    re.MULTILINE,
)
_BRANCH_REQUEST = re.compile(
    rf"{_INSTRUCTION_START}"
    rf"(?i:(?:create |work on |commit on |check out |switch to |on )"
    rf"{_BRANCH_PHRASE})`({_BRANCH_NAME})`",
    re.MULTILINE,
)
_PUSH_REQUEST = re.compile(
    rf"(?:\b(?i:{_PUSH_TO} (?:{_BRANCH_PHRASE})?)(?=`{_BRANCH_NAME}`)"
    rf"|{_INSTRUCTION_START}(?i:{_PUSH_TO}) "
    rf"(?:(?i:{_BRANCH_PHRASE})|(?=[`\"']?(?:refs/heads/|agent/))))"
    rf"[`\"']?({_BRANCH_NAME})(?=[`\"']|[.,;!?](?: |$)|$)",
    re.MULTILINE,
)


def unfetched_prompt_refs(prompt: str, fetch_refs: list[str]) -> list[str]:
    """Return refs missing from the fetch list in order of first mention."""
    return [
        ref
        for ref in dict.fromkeys(_PROMPT_REF.findall(prompt))
        if ref not in fetch_refs
    ]


def _instructions(pattern: re.Pattern[str], prose: str) -> Iterator[re.Match[str]]:
    return (
        match
        for match in pattern.finditer(prose)
        if not brief.prohibiting(prose, match.start())
    )


def _unfetched_explicit_refs(
    prompt: str, named_refs: list[str | None], fetch_refs: list[str]
) -> list[str]:
    explicit = [
        ref
        for ref in _PROMPT_REF.findall(prompt)
        if ref.startswith(("refs/pull/", "refs/heads/agent/"))
    ]
    explicit.extend(
        f"refs/heads/{ref}" for ref in named_refs if ref and ref.startswith("agent/")
    )
    return [ref for ref in dict.fromkeys(explicit) if ref not in fetch_refs]


def _build_branch_problems(prose: str, allowed: str) -> list[str]:
    for pattern in (_BRANCH_COMMAND, _BRANCH_REQUEST, _PUSH_REQUEST):
        for match in _instructions(pattern, prose):
            branch = match[1].removeprefix("refs/heads/")
            if branch != allowed:
                return [
                    (
                        f"the brief targets branch {branch}. "
                        f"The driver owns the build branch. Use {allowed}"
                    )
                ]
    return []


def prompt_launch_problems(
    prompt: str,
    profile: Profile,
    fetch_refs: list[str],
    start_ref: str | None,
    run: str,
) -> list[str]:
    """Check explicit ref uses and branch update instructions in a launch brief."""
    if profile is Profile.BUILD:
        prompt = _REFUSED_COMMAND_EXAMPLE.sub("the example", prompt)
    # Fold line wraps within a paragraph without changing ref case.
    prose = "\n".join(
        " ".join(block.split()) for block in brief.PROSE_BREAK.split(prompt)
    )
    if profile is Profile.BUILD:
        branch_problems = _build_branch_problems(prose, f"agent/{run}")
        if branch_problems:
            return branch_problems
    updates = [match[1] for match in _instructions(_UPDATE, prose)]
    named_refs = [match[1] for match in _instructions(_REF_USE, prose)] + updates
    problems = [
        f"the brief names {ref}. Add `--fetch-ref {ref}`"
        for ref in _unfetched_explicit_refs(prompt, named_refs, fetch_refs)
    ]
    if profile is Profile.BUILD and start_ref is None and updates:
        target = updates[0] or "<branch>"
        ref = f"refs/heads/{target.removeprefix('refs/heads/')}"
        problems.append(
            "the brief asks to update a PR branch. "
            f"Use `--fetch-ref {ref} --start-ref {ref}`"
        )
    return problems


def check_name(name: str) -> None:
    if not NAME.fullmatch(name):
        message = f"run name {name!r} must match {NAME.pattern}"
        raise Invalid(message)


def select_host(settings: Settings, name: str, account: str | None) -> Host:
    for host in settings.hosts:
        if host.name == name:
            chosen = host.account if account is None else account
            if chosen not in host.accounts:
                message = f"account {chosen!r} isn't one of the accounts of {name}"
                raise Invalid(message)
            return replace(host, account=chosen)
    message = f"host {name!r} isn't configured"
    raise Invalid(message)


def check(
    run: RunRequest,
    settings: Settings,
    host: Host,
    *,
    existing_name: str | None,
    ref: str | None,
) -> RunRequest:
    if existing_name is not None:
        message = (
            "run metadata already exists. "
            f"Run `tbhbdev alpha agent clean {existing_name}`"
        )
        raise Invalid(message)
    if ref is not None and run.profile is Profile.BUILD:
        message = (
            "--ref applies only to a review run. "
            "For a build run use --fetch-ref REF --start-ref REF"
        )
        raise Invalid(message)
    if not ref and run.profile is Profile.REVIEW:
        message = "a review run needs --ref with the full commit hash to review"
        raise Invalid(message)
    if run.harness is Harness.CLAUDE:
        name = (
            settings.claude.default_profile
            if run.claude_profile is None
            else run.claude_profile
        )
        profile = config.find_profile(settings.claude.profiles, name)
        if profile is None:
            message = f"claude profile {name!r} isn't defined"
            raise Invalid(message)
        run = replace(run, claude_profile=profile.name)
    request.validate(run, host_allows_unsandboxed=host.unsandboxed)
    return run


def same_version(local: str, remote: str) -> None:
    if local != remote:
        message = f"version mismatch: driver {local!r}; host {remote!r}"
        raise Invalid(message)


def resolve_ref(ref: str, text: str) -> str:
    if COMMIT.fullmatch(ref):
        return ref
    lines = [line.split() for line in text.splitlines()]
    # Prefer a peeled tag; a branch and tag may name the same commit.
    records = {name: sha for sha, name in lines}
    commits = {sha for name, sha in records.items() if name + "^{}" not in records}
    if len(commits) != 1 or not COMMIT.fullmatch(next(iter(commits))):
        message = f"ref {ref!r} doesn't resolve to one commit"
        raise Invalid(message)
    return commits.pop()


def launch(rc: int | None, *, launched: bool, expired: bool) -> str:
    if rc is not None:
        return "finished"
    if launched:
        return "launched"
    return "deadline" if expired else "poll"


def launch_deadline(phase: str, deadline: float, shim_seconds: int) -> float:
    return deadline + shim_seconds if phase == "shims" else deadline


def state(rc: int | None, *, session: bool) -> str:
    if rc is not None:
        return str(rc)
    return "running" if session else "unknown"


def wait(
    rc: int | None, *, session: bool, failed_seconds: float, unreachable: float
) -> int | None:
    if failed_seconds >= unreachable:
        return 75
    if rc is not None:
        return rc
    return None if session else 1


def kill(rc: int | None, *, session: bool, expired: bool) -> str:
    if rc is not None:
        return "finished"
    if not expired:
        return "poll"
    return "refuse" if session else "write"


def clean(rc: int | None, *, session: bool, force: bool) -> None:
    if session:
        message = "run is running; clean refused"
        raise Invalid(message)
    if rc is None and not force:
        message = "run is unknown; clean requires --force"
        raise Invalid(message)


def wait_kind(kind: RunKind) -> None:
    if kind is not RunKind.EXEC:
        message = "wait applies only to an exec run"
        raise Invalid(message)


def wait_seconds(value: str, name: str) -> float:
    try:
        seconds = float(value)
    except ValueError as exc:
        message = f"{name} must be positive seconds"
        raise Invalid(message) from exc
    if seconds <= 0 or not math.isfinite(seconds):
        message = f"{name} must be positive seconds"
        raise Invalid(message)
    return seconds


def check_model(harness: Harness, model: str, *, allow_non_gemini: bool) -> None:
    if (
        harness is Harness.AGY
        and not model.startswith("gemini-")
        and not allow_non_gemini
    ):
        message = "agy requires a gemini-* model or --allow-non-gemini"
        raise Invalid(message)


def answer_state(rc: int | None) -> str:
    return "unknown" if rc is None else str(rc)

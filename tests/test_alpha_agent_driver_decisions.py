"""Coordinator rules use literal facts and never inspect the outside world."""

from __future__ import annotations

import dataclasses

import pytest
from agent_example import LINUX, settings
from test_alpha_agent_request import BUILD, CLAUDE

from tbhbdev.alpha.agent.decisions import driver, request
from tbhbdev.alpha.agent.model import Harness, Invalid, Profile, RunKind

CONFIG = settings()
RUN = request.parse(BUILD, host_allows_unsandboxed=True)
COMMIT = "0123456789abcdef0123456789abcdef01234567"


@pytest.mark.parametrize("name", ["sample", "1", "A-z_0", "a" * 64])
def test_name(name: str) -> None:
    assert driver.check_name(name) is None


@pytest.mark.parametrize("name", ["", "../bad", "a b", "a" * 65])
def test_bad_name(name: str) -> None:
    with pytest.raises(Invalid, match=r"run name .* must match"):
        driver.check_name(name)


@pytest.mark.parametrize("account", [None, "agent", "ops"])
def test_host(account: str | None) -> None:
    host = driver.select_host(CONFIG, "example-linux", account)
    assert host == dataclasses.replace(LINUX, account=account or "agent")


@pytest.mark.parametrize(
    ("host", "account", "message"),
    [
        ("other", None, "host 'other' isn't configured"),
        (
            "example-linux",
            "other",
            "account 'other' isn't one of the accounts of example-linux",
        ),
    ],
)
def test_bad_host(host: str, account: str | None, message: str) -> None:
    with pytest.raises(Invalid) as caught:
        driver.select_host(CONFIG, host, account)
    assert str(caught.value) == message


@pytest.mark.parametrize(
    ("exists", "ref", "message"),
    [
        (True, None, "run metadata already exists; clean first"),
        (False, "main", "--ref applies only to a review run"),
    ],
)
def test_check_refuses(exists: bool, ref: str | None, message: str) -> None:
    with pytest.raises(Invalid) as caught:
        driver.check(RUN, CONFIG, LINUX, ref=ref, exists=exists)
    assert str(caught.value) == message


@pytest.mark.parametrize("profile", [None, "Primary", "primary"])
def test_profile(profile: str | None) -> None:
    run = dataclasses.replace(
        request.parse(CLAUDE, host_allows_unsandboxed=True), claude_profile=profile
    )
    assert driver.check(
        run, CONFIG, LINUX, ref=None, exists=False
    ) == dataclasses.replace(run, claude_profile="primary")


def test_unknown_profile() -> None:
    run = dataclasses.replace(RUN, harness=Harness.CLAUDE, claude_profile="Missing")
    with pytest.raises(Invalid, match="claude profile 'Missing' isn't defined"):
        driver.check(run, CONFIG, LINUX, ref=None, exists=False)


def test_review_ref_allowed() -> None:
    run = dataclasses.replace(RUN, profile=Profile.REVIEW, commit=COMMIT)
    assert driver.check(run, CONFIG, LINUX, ref="topic", exists=False) == run
    assert driver.check(RUN, CONFIG, LINUX, ref=None, exists=False) == RUN


@pytest.mark.parametrize(
    ("local", "remote", "matches"),
    [("one", "one", True), ("one", "two", False), ("", "one", False)],
)
def test_version(local: str, remote: str, matches: bool) -> None:
    if matches:
        assert driver.same_version(local, remote) is None
    else:
        with pytest.raises(Invalid) as caught:
            driver.same_version(local, remote)
        assert (
            str(caught.value) == f"version mismatch: driver {local!r}; host {remote!r}"
        )


@pytest.mark.parametrize(
    ("ref", "text", "result"),
    [
        (COMMIT, "", COMMIT),
        ("a" * 64, "", "a" * 64),
        ("topic", f"{COMMIT}\trefs/heads/topic\n", COMMIT),
        ("tag", f"{'a' * 40}\trefs/tags/tag\n{COMMIT}\trefs/tags/tag^{{}}\n", COMMIT),
        ("topic", "", None),
        ("topic", "bad refs/heads/topic\n", None),
        ("topic", f"{COMMIT} refs/heads/topic\n{'b' * 40} refs/tags/topic\n", None),
    ],
)
def test_ref(ref: str, text: str, result: str | None) -> None:
    if result is None:
        with pytest.raises(Invalid) as caught:
            driver.resolve_ref(ref, text)
        assert str(caught.value) == f"ref {ref!r} doesn't resolve to one commit"
    else:
        assert driver.resolve_ref(ref, text) == result


@pytest.mark.parametrize(
    ("rc", "launched", "expired", "result"),
    [
        (0, False, False, "finished"),
        (7, True, True, "finished"),
        (None, True, False, "launched"),
        (None, True, True, "launched"),
        (None, False, False, "poll"),
        (None, False, True, "deadline"),
    ],
)
def test_launch(rc: int | None, launched: bool, expired: bool, result: str) -> None:
    assert driver.launch(rc, launched=launched, expired=expired) == result


@pytest.mark.parametrize(
    ("rc", "session", "result"),
    [
        (0, True, "0"),
        (7, False, "7"),
        (None, True, "running"),
        (None, False, "unknown"),
    ],
)
def test_state(rc: int | None, session: bool, result: str) -> None:
    assert driver.state(rc, session=session) == result


@pytest.mark.parametrize(
    ("rc", "session", "elapsed", "result"),
    [
        (0, True, 0, 0),
        (7, False, 239, 7),
        (None, True, 0, None),
        (None, False, 0, 1),
        (None, True, 240, 75),
        (None, True, 241, 75),
    ],
)
def test_wait(
    rc: int | None, session: bool, elapsed: float, result: int | None
) -> None:
    assert (
        driver.wait(rc, session=session, failed_seconds=elapsed, unreachable=240)
        == result
    )


@pytest.mark.parametrize(
    ("rc", "session", "expired", "result"),
    [
        (0, True, False, "finished"),
        (7, False, True, "finished"),
        (None, False, False, "poll"),
        (None, True, False, "poll"),
        (None, False, True, "write"),
        (None, True, True, "refuse"),
    ],
)
def test_kill(rc: int | None, session: bool, expired: bool, result: str) -> None:
    assert driver.kill(rc, session=session, expired=expired) == result


@pytest.mark.parametrize(
    ("rc", "session", "force", "message"),
    [
        (0, True, False, "run is running; clean refused"),
        (0, True, True, "run is running; clean refused"),
        (None, False, False, "run is unknown; clean requires --force"),
        (None, False, True, None),
        (0, False, False, None),
        (7, False, True, None),
    ],
)
def test_clean(rc: int | None, session: bool, force: bool, message: str | None) -> None:
    if message is None:
        assert driver.clean(rc, session=session, force=force) is None
    else:
        with pytest.raises(Invalid) as caught:
            driver.clean(rc, session=session, force=force)
        assert str(caught.value) == message


@pytest.mark.parametrize(
    ("unpushed", "force", "result"),
    [
        (False, False, True),
        (False, True, True),
        (True, False, False),
        (True, True, True),
    ],
)
def test_branch(unpushed: bool, force: bool, result: bool) -> None:
    assert driver.delete_branch(unpushed=unpushed, force=force) is result


@pytest.mark.parametrize("kind", RunKind)
def test_wait_kind(kind: RunKind) -> None:
    if kind is RunKind.EXEC:
        assert driver.wait_kind(kind) is None
    else:
        with pytest.raises(Invalid) as caught:
            driver.wait_kind(kind)
        assert str(caught.value) == "wait applies only to an exec run"


@pytest.mark.parametrize(
    ("value", "result"),
    [
        ("1", 1),
        ("0.1", 0.1),
        ("0", None),
        ("-1", None),
        ("nan", None),
        ("inf", None),
        ("bad", None),
    ],
)
def test_wait_seconds(value: str, result: float | None) -> None:
    if result is None:
        with pytest.raises(Invalid) as caught:
            driver.wait_seconds(value, "EXAMPLE")
        assert str(caught.value) == "EXAMPLE must be positive seconds"
    else:
        assert driver.wait_seconds(value, "EXAMPLE") == result


@pytest.mark.parametrize(
    ("harness", "model", "allow", "valid"),
    [
        (Harness.CODEX, "example", False, True),
        (Harness.CLAUDE, "example", False, True),
        (Harness.AGY, "gemini-example", False, True),
        (Harness.AGY, "example", True, True),
        (Harness.AGY, "example", False, False),
        (Harness.AGY, "gemini", False, False),
    ],
)
def test_model(harness: Harness, model: str, allow: bool, valid: bool) -> None:
    if valid:
        assert driver.check_model(harness, model, allow_non_gemini=allow) is None
    else:
        with pytest.raises(Invalid) as caught:
            driver.check_model(harness, model, allow_non_gemini=allow)
        assert (
            str(caught.value) == "agy requires a gemini-* model or --allow-non-gemini"
        )


def test_unsandboxed_host_permission_reaches_validation() -> None:
    run = dataclasses.replace(RUN, unsandboxed=True, authorized_by="o/r#1")
    assert driver.check(run, CONFIG, LINUX, ref=None, exists=False) == run
    with pytest.raises(Invalid, match="isn't enabled on this host"):
        driver.check(
            run,
            CONFIG,
            dataclasses.replace(LINUX, unsandboxed=False),
            ref=None,
            exists=False,
        )


def test_peeled_tag_does_not_hide_an_ambiguous_branch() -> None:
    text = (
        f"{'b' * 40} refs/heads/topic\n{'a' * 40} refs/tags/topic\n"
        f"{COMMIT} refs/tags/topic^{{}}\n"
    )
    with pytest.raises(Invalid) as caught:
        driver.resolve_ref("topic", text)
    assert str(caught.value) == "ref 'topic' doesn't resolve to one commit"


@pytest.mark.parametrize(("rc", "label"), [(None, "unknown"), (0, "0"), (7, "7")])
def test_answer_state(rc: int | None, label: str) -> None:
    assert driver.answer_state(rc) == label


def test_ref_branch_and_tag_may_share_a_commit() -> None:
    refs = f"{COMMIT} refs/heads/topic\n{COMMIT} refs/tags/topic\n"
    assert driver.resolve_ref("topic", refs) == COMMIT

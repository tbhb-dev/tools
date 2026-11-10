"""Pure trust decisions over coordinator audit records."""

from __future__ import annotations

from dataclasses import replace

import pytest

from tbhbdev.alpha.agent.decisions import taint
from tbhbdev.alpha.agent.model import Invalid, Profile

HOST = "example-linux"
ACCOUNT = "agent"
START = "2026-11-10T12:00:00+00:00"
LATER = "2026-11-10T13:00:00Z"
LAST = "2026-11-10T14:00:00+00:00"
RECORD = taint.Taint(HOST, ACCOUNT, "sample", "operator-chat-2026-11-10", START)
CLEARANCE = taint.Clearance(HOST, ACCOUNT, "Account rebuilt", LATER)


def test_review_refused_and_build_labelled() -> None:
    dirty = taint.state([RECORD], [], HOST, ACCOUNT)
    assert dirty == taint.State(HOST, ACCOUNT, RECORD)
    assert taint.request_flag(dirty) is True
    assert taint.check_launch(Profile.REVIEW, unsandboxed=False, tainted=dirty) == (
        taint.Launch(
            65,
            "review runs refused: example-linux account agent ran unsandboxed "
            "(sample); clear with: tbhbdev alpha agent clear-taint "
            "example-linux agent --reason …",
        )
    )
    for full in (False, True):
        assert taint.check_launch(
            Profile.BUILD, unsandboxed=full, tainted=dirty
        ) == taint.Launch(label="untrusted-host")


def test_clean_launch_and_unsandboxed_label() -> None:
    clean = taint.state([], [], HOST, ACCOUNT)
    assert clean == taint.State(HOST, ACCOUNT)
    assert taint.request_flag(clean) is False
    for profile in Profile:
        assert taint.check_launch(
            profile, unsandboxed=False, tainted=clean
        ) == taint.Launch(0, "", None)
    assert taint.check_launch(
        Profile.BUILD, unsandboxed=True, tainted=clean
    ) == taint.Launch(label="untrusted-host")


def test_clearance_then_later_taint() -> None:
    newer = replace(RECORD, run="later", started=LAST)
    assert not taint.state([RECORD], [CLEARANCE], HOST, ACCOUNT).tainted
    assert not taint.state(
        [RECORD], [replace(CLEARANCE, time=START)], HOST, ACCOUNT
    ).tainted
    assert taint.state([newer, RECORD], [CLEARANCE], HOST, ACCOUNT).taint == newer
    assert not taint.state(
        [newer, RECORD], [replace(CLEARANCE, time=LAST), CLEARANCE], HOST, ACCOUNT
    ).tainted
    # Lexical ordering differs between these equivalent UTC spellings.
    assert not taint.state(
        [replace(RECORD, started=LATER)],
        [replace(CLEARANCE, time="2026-11-10T13:00:00.000000+00:00")],
        HOST,
        ACCOUNT,
    ).tainted


@pytest.mark.parametrize(
    ("host", "account"), [("other-host", ACCOUNT), (HOST, "other")]
)
def test_identities_independent(host: str, account: str) -> None:
    assert not taint.state([RECORD], [CLEARANCE], host, account).tainted
    other = replace(CLEARANCE, host=host, account=account)
    assert taint.state([RECORD], [other], HOST, ACCOUNT).taint == RECORD
    assert not taint.state([], [other], host, account).tainted


@pytest.mark.parametrize(
    "text", ["", " ", "x" * 201, "a\nb", "a\rb", "a\tb", "a\x1bb", "a\x00b", "a\u202eb"]
)
def test_bad_terminal_text_refused(text: str) -> None:
    with pytest.raises(
        Invalid,
        match=r"^audit text must be one printable line of up to 200 characters$",
    ):
        replace(CLEARANCE, reason=text)
    with pytest.raises(Invalid, match="audit text"):
        replace(RECORD, authorized_by=text)


@pytest.mark.parametrize("text", ["x", "x" * 200, "Rebuilt: café"])
def test_printable_text_boundaries(text: str) -> None:
    assert replace(CLEARANCE, reason=text).reason == text
    assert replace(RECORD, authorized_by=text).authorized_by == text


@pytest.mark.parametrize(
    "value",
    [
        "garbage",
        "2026-11-10",
        "2026-11-10T12:00:00",
        "2026-11-10 12:00:00Z",
        "2026-11-10T12:00:00+01:00",
    ],
)
def test_time_requires_utc_iso(value: str) -> None:
    with pytest.raises(ValueError, match=r"isoformat|audit time") as caught:
        replace(RECORD, started=value)
    if value != "garbage":
        assert str(caught.value) == "audit time must be UTC ISO-8601"
    with pytest.raises(ValueError, match=r"isoformat|audit time"):
        replace(CLEARANCE, time=value)


@pytest.mark.parametrize(("host", "account"), [("../bad", ACCOUNT), (HOST, "../bad")])
def test_bad_identity(host: str, account: str) -> None:
    with pytest.raises(Invalid, match=r"^invalid taint host or account$"):
        taint.state([], [], host, account)
    with pytest.raises(Invalid):
        replace(RECORD, host=host, account=account)
    with pytest.raises(Invalid):
        replace(CLEARANCE, host=host, account=account)


def test_bad_run_name() -> None:
    with pytest.raises(Invalid, match=r"^invalid taint run name$"):
        replace(RECORD, run="../bad")


def test_failed_store_is_tainted() -> None:
    dirty = taint.State(HOST, ACCOUNT, problem="wrong ownership")
    assert taint.request_flag(dirty) is True
    decision = taint.check_launch(Profile.REVIEW, unsandboxed=False, tainted=dirty)
    assert decision.status == 65
    assert decision.label is None
    assert decision.message == (
        "review runs refused: example-linux account agent has invalid taint "
        "records (wrong ownership); clear with: tbhbdev alpha agent clear-taint "
        "example-linux agent --reason …"
    )
    assert (
        taint.check_launch(Profile.BUILD, unsandboxed=False, tainted=dirty).label
        == "untrusted-host"
    )

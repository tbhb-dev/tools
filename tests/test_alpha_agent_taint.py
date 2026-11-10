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
CLEARANCE = taint.Clearance(HOST, "Host rebuilt", LATER)


def test_review_refused_and_build_labelled() -> None:
    dirty = taint.state([RECORD], HOST)
    assert dirty == taint.State(HOST, "tainted", RECORD)
    assert taint.request_flag(dirty) is True
    assert taint.check_launch(Profile.REVIEW, tainted=dirty) == (
        taint.Launch(
            65,
            "review runs refused: example-linux host=tainted; "
            "clear with: tbhbdev alpha agent clear-taint "
            "example-linux --reason …",
        )
    )
    assert taint.check_launch(Profile.BUILD, tainted=dirty) == taint.Launch(
        label="untrusted-host"
    )


def test_clean_launch() -> None:
    clean = taint.state([CLEARANCE], HOST)
    assert clean == taint.State(HOST, "clean")
    assert taint.request_flag(clean) is False
    for profile in Profile:
        assert taint.check_launch(profile, tainted=clean) == taint.Launch(0, "", None)


def test_append_order_ignores_clock_and_account() -> None:
    newer = replace(RECORD, run="later", started=LAST, account="ops")
    assert taint.state([newer, CLEARANCE], HOST).status == "clean"
    assert taint.state([CLEARANCE, RECORD], HOST).taint == RECORD
    assert taint.state([newer, CLEARANCE, RECORD], HOST).taint == RECORD
    assert taint.state([RECORD], "other-host").status == "unknown"
    assert taint.state([], HOST).status == "unknown"
    assert taint.request_flag(taint.state([], HOST)) is True


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
        taint.identity(host, account)
    with pytest.raises(Invalid):
        replace(RECORD, host=host, account=account)
    with pytest.raises(Invalid):
        taint.identity(host, account)


def test_bad_run_name() -> None:
    with pytest.raises(Invalid, match=r"^invalid taint run name$"):
        replace(RECORD, run="../bad")


def test_failed_store_is_tainted() -> None:
    dirty = taint.State(HOST, "tainted", problem="wrong ownership")
    assert taint.request_flag(dirty) is True
    decision = taint.check_launch(Profile.REVIEW, tainted=dirty)
    assert decision.status == 65
    assert decision.label is None
    assert decision.message == (
        "review runs refused: example-linux host=tainted; "
        "clear with: tbhbdev alpha agent clear-taint example-linux --reason …"
    )
    assert taint.check_launch(Profile.BUILD, tainted=dirty).label == "untrusted-host"


def test_state_rejects_bad_host() -> None:
    with pytest.raises(Invalid, match="invalid taint host or account"):
        taint.state([], "../bad")

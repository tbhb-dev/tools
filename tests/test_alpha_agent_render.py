"""Report text is a pure function of literal facts."""

from tbhbdev.alpha.agent import render


def test_launch() -> None:
    assert (
        render.launched("example", "example-linux", "agent")
        == "example: launched host=example-linux account=agent"
    )


def test_status() -> None:
    assert render.status(
        "example",
        phase="finishing",
        rc="0",
        target=("example-linux", "agent"),
        started="2026-11-09T00:00:00+00:00",
    ) == (
        "example: phase=finishing rc=0 host=example-linux "
        "account=agent started=2026-11-09T00:00:00+00:00"
    )


def test_inspection_warning() -> None:
    assert render.INSPECTION_WARNING == (
        "Don't run git inside a run clone: it is agent-written. "
        "Use `tbhbdev alpha agent log|diff NAME`."
    )

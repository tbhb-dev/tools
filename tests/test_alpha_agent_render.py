"""Report text is a pure function of literal facts."""

from tbhbdev.alpha.agent import render


def test_brief_and_launch() -> None:
    assert render.brief("example\n") == (
        "Report results only in your final answer. "
        "Never message Claude sessions, session sockets or other agents directly."
        "\n\nexample\n"
    )
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

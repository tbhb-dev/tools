"""Command dispatch closes connections and reports failures without credentials."""

import pytest
from analytics_fakes import FakeConnection, answer

from tbhbdev import analytics
from tbhbdev.analytics import catalog, database, settings
from tbhbdev.analytics.model import Catalog, Migration, Settings


@pytest.mark.parametrize(
    "argv", [[], ["contract"], ["unknown"], ["migrate", "--unknown"]]
)
def test_usage(argv: list[str], capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as caught:
        analytics.main(argv, "tool analytics")
    assert caught.value.code == 2
    assert capsys.readouterr().err.startswith("usage: tool analytics")


FILES = {"reader_password_file": "reader", "grafana_password_file": "grafana"}


@pytest.mark.parametrize("dry_run", [False, True])
@pytest.mark.parametrize("configured", [False, True])
def test_migrate(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    dry_run: bool,
    configured: bool,
) -> None:
    fake = FakeConnection()
    options = Settings(**FILES) if configured else Settings()
    calls: list[tuple[database.Connection, Settings, bool]] = []

    def migrate(
        connection: database.Connection, configured: Settings, *, dry_run: bool
    ) -> tuple[Migration, ...]:
        calls.append((connection, configured, dry_run))
        return (Migration("0007_next.sql", 7),)

    def connect(configured: Settings) -> FakeConnection:
        assert configured is options
        return fake

    def unexpected(_connection: database.Connection) -> None:
        pytest.fail("migrate queried catalog")

    monkeypatch.setattr(settings, "load", lambda: options)
    monkeypatch.setattr(
        database,
        "connect",
        connect,
    )
    monkeypatch.setattr(database, "migrate", migrate)
    monkeypatch.setattr(catalog, "read", unexpected)
    assert analytics.main(["migrate", *(["--dry-run"] if dry_run else [])], "tool") == 0
    assert calls == [(fake, options, dry_run)]
    assert fake.closed
    output = capsys.readouterr()
    assert (
        output.out == f"analytics: 1 {'pending' if dry_run else 'applied'} migrations\n"
        "7: 0007_next.sql\n"
        + (
            ""
            if configured
            else "role analytics_reader has no password "
            "(reader_password_file is not configured)\n"
            "role grafana_reader has no password "
            "(grafana_password_file is not configured)\n"
        )
    )
    assert output.err == ""


@pytest.mark.parametrize("equal", [False, True])
def test_contract_check(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], equal: bool
) -> None:
    fake = FakeConnection()
    monkeypatch.setattr(settings, "load", Settings)
    monkeypatch.setattr(database, "connect", answer(fake))
    monkeypatch.setattr(
        catalog, "contract", lambda: Catalog() if equal else Catalog(("missing",))
    )
    assert analytics.main(["contract", "check"], "tool") == (0 if equal else 1)
    assert fake.closed
    assert len(fake.calls) == 1
    output = capsys.readouterr()
    assert output.out == (
        "analytics: catalog matches contract\n"
        if equal
        else "analytics: missing schema: missing\n"
        "analytics: catalog differs from contract\n"
    )
    assert output.err == ""


@pytest.mark.parametrize("stage", ["load", "connect", "migrate", "read", "close"])
def test_failure(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], stage: str
) -> None:
    fake = FakeConnection()

    def fail(*_args: object, **_kwargs: object) -> None:
        message = "permission denied password sentinel"
        raise ValueError(message)

    monkeypatch.setattr(settings, "load", fail if stage == "load" else Settings)
    monkeypatch.setattr(
        database, "connect", fail if stage == "connect" else answer(fake)
    )
    monkeypatch.setattr(
        database,
        "migrate",
        fail if stage == "migrate" else lambda *_args, **_kwargs: (),
    )
    if stage == "read":
        monkeypatch.setattr(catalog, "read", fail)
    if stage == "close":
        monkeypatch.setattr(fake, "close", fail)
    argv = ["contract", "check"] if stage == "read" else ["migrate"]
    assert analytics.main(argv, "tool") == 1
    assert fake.closed is (stage in {"migrate", "read"})
    output = capsys.readouterr()
    assert output.out == (
        "analytics: 0 applied migrations\n"
        "role analytics_reader has no password "
        "(reader_password_file is not configured)\n"
        "role grafana_reader has no password "
        "(grafana_password_file is not configured)\n"
        if stage == "close"
        else ""
    )
    assert (
        output.err
        == "migration diagnostic: permission denied [redacted output] [credential]\n"
        "analytics: command failed\n"
    )

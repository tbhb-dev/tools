"""The public scan_text wrapper turns the scan's refusals into ScanRefused."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from fake_tool import write_private_config

from tbhbdev import gitleaks_check
from tbhbdev.alpha import secret_scan

if TYPE_CHECKING:
    from pathlib import Path


def test_a_clean_scan_returns_none_and_passes_every_argument(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[tuple[object, ...]] = []

    def scan(
        text: str, what: str, *, root: Path, gitleaks: str, visibility: str
    ) -> None:
        calls.append((text, what, root, gitleaks, visibility))

    monkeypatch.setattr(secret_scan, "scan", scan)
    assert (
        gitleaks_check.scan_text(
            "hello", "the body", root=tmp_path, gitleaks="gl", visibility="PRIVATE"
        )
        is None
    )
    assert calls == [("hello", "the body", tmp_path, "gl", "PRIVATE")]


def test_a_refusal_becomes_scan_refused_with_the_same_message(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def scan(*_args: object, **_kwargs: object) -> None:
        message = "cannot scan the body: finding rule-1 at line 2"
        raise SystemExit(message)

    monkeypatch.setattr(secret_scan, "scan", scan)
    with pytest.raises(gitleaks_check.ScanRefused) as caught:
        gitleaks_check.scan_text(
            "x", "the body", root=tmp_path, gitleaks="gl", visibility="PUBLIC"
        )
    assert str(caught.value) == "cannot scan the body: finding rule-1 at line 2"
    assert caught.value.__cause__ is None


def test_a_missing_private_config_fails_closed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "empty-home"))
    with pytest.raises(gitleaks_check.ScanRefused) as caught:
        gitleaks_check.scan_text(
            "clean text", "the body", root=tmp_path, gitleaks="gl", visibility="PUBLIC"
        )
    assert "the private gitleaks config" in str(caught.value)
    assert "cannot be read" in str(caught.value)


def test_findings_keep_only_the_rule_and_line(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def scan(_text: str, what: str, **_options: object) -> None:
        raise secret_scan.FindingRefusal(what, [(2, "fixture-rule")])

    monkeypatch.setattr(secret_scan, "scan", scan)
    with pytest.raises(gitleaks_check.ScanRefused) as caught:
        gitleaks_check.scan_text(
            "matched text",
            "the body",
            root=tmp_path,
            gitleaks="gl",
            visibility="PUBLIC",
        )
    assert caught.value.locations == "rule fixture-rule on line 2"
    assert "matched text" not in str(caught.value)


def test_scan_refused_is_a_runtime_error() -> None:
    assert issubclass(gitleaks_check.ScanRefused, RuntimeError)


@pytest.mark.parametrize("make_executable", [False, True])
def test_a_scanner_that_cannot_run_is_refused_without_the_text(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, make_executable: bool
) -> None:
    repo = tmp_path / "checkout"
    repo.mkdir()
    (repo / ".gitleaks.toml").write_text("")
    write_private_config(tmp_path / "scanner-home")
    monkeypatch.setenv("HOME", str(tmp_path / "scanner-home"))
    scanner = tmp_path / "bin" / "scanner"
    if make_executable:
        scanner.parent.mkdir()
        scanner.write_text("not a program\n")
        scanner.chmod(0o600)
    with pytest.raises(gitleaks_check.ScanRefused) as caught:
        gitleaks_check.scan_text(
            "the-scanned-text", "the body", root=repo, gitleaks=str(scanner),
            visibility="PUBLIC",
        )  # fmt: skip
    message = str(caught.value)
    assert message.startswith("cannot scan the body for secrets: the scanner ")
    assert str(scanner) in message
    assert "cannot run (" in message
    assert "the-scanned-text" not in message
    assert caught.value.__cause__ is None

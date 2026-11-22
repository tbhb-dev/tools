"""The public check_prose wrapper turns lint findings into ProseRefused."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from tbhbdev import prose_check
from tbhbdev.alpha import pull_request

if TYPE_CHECKING:
    from collections.abc import Sequence


def fake_lint(
    monkeypatch: pytest.MonkeyPatch, result: list[tuple[int, str]] | None
) -> list[tuple[object, ...]]:
    calls: list[tuple[object, ...]] = []

    def lint(
        rendered: Sequence[str], first: int, *, path: Path
    ) -> list[tuple[int, str]]:
        calls.append((list(rendered), first, path, Path.cwd()))
        if result is None:
            message = "missing lint config .vale.ini"
            raise SystemExit(message)
        return result

    monkeypatch.setattr(pull_request, "lint", lint)
    return calls


def test_clean_text_returns_none_and_forwards_arguments(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls = fake_lint(monkeypatch, [])
    assert prose_check.check_prose("one\ntwo", "the body", root=tmp_path) is None
    assert calls == [(["one", "two"], 1, pull_request.DRAFT, tmp_path.resolve())]


def test_the_working_directory_is_restored(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    before = Path.cwd()
    fake_lint(monkeypatch, [])
    prose_check.check_prose("x", "the body", root=tmp_path)
    assert Path.cwd() == before


def test_findings_become_one_line_each(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fake_lint(monkeypatch, [(1, "vale Google.We: avoid we"), (3, "cspell: Unknown")])
    with pytest.raises(prose_check.ProseRefused) as caught:
        prose_check.check_prose("a", "the body", root=tmp_path)
    assert str(caught.value) == (
        "the body\nline 1: vale Google.We: avoid we\nline 3: cspell: Unknown"
    )


def test_a_missing_config_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fake_lint(monkeypatch, None)
    with pytest.raises(prose_check.ProseRefused) as caught:
        prose_check.check_prose("a", "the body", root=tmp_path)
    assert str(caught.value) == "the body\nmissing lint config .vale.ini"


def test_prose_refused_is_a_runtime_error() -> None:
    assert issubclass(prose_check.ProseRefused, RuntimeError)


def test_a_linter_that_cannot_run_is_refused_without_the_text(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def broken(*_args: object, **_kwargs: object) -> list[tuple[int, str]]:
        raise FileNotFoundError(2, "No such file or directory")

    monkeypatch.setattr(pull_request, "lint", broken)
    with pytest.raises(prose_check.ProseRefused) as caught:
        prose_check.check_prose("the-checked-text", "the body", root=tmp_path)
    assert str(caught.value) == (
        "cannot check the body for prose: a linter cannot run "
        "(No such file or directory)"
    )
    assert caught.value.__cause__ is None

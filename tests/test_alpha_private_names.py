"""Tests for the private repository name check in tbhbdev alpha pr check."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from tbhbdev.alpha import private_names as pn
from tbhbdev.alpha import pull_request as pr

if TYPE_CHECKING:
    from pathlib import Path

NAMES = ["example-lab"]


def tokens(lines: list[str], names: list[str] | None = None) -> list[str]:
    found = pn.private_name_problems(lines, names or [])
    return [message.split("'")[1] for _, message in found]


def test_suffix_rule_flags_without_config() -> None:
    assert tokens(["see example-private for details"]) == ["example-private"]


def test_configured_name_is_flagged() -> None:
    assert tokens(["moved to example-lab today"], NAMES) == ["example-lab"]
    assert tokens(["moved to example-lab today"]) == []


def test_message_says_private_and_generic() -> None:
    [(_, message)] = pn.private_name_problems(["example-private"], [])
    assert message == (
        "'example-private' names a private repository; describe it generically instead"
    )


@pytest.mark.parametrize(
    "line",
    [
        "example-private-tools",
        "my-example-lab",
        "example-lab2",
        "example-labs",
        "example_example-lab",
        "example.example-lab",
        "example-privately",
        "--private",
        "-private",
        "the -private suffix",
        "example-private.git.x",
    ],
)
def test_longer_tokens_and_leading_hyphens_are_not_flagged(line: str) -> None:
    assert tokens([line], NAMES) == []


@pytest.mark.parametrize(
    ("line", "found"),
    [
        ("(example-private)", ["example-private"]),
        ("example-private.", ["example-private"]),
        ("`example-private`, x", ["example-private"]),
        ("tbhb-dev/example-private/x", ["example-private"]),
        ("example-lab.dev", []),
    ],
)
def test_punctuation_bounds_a_token(line: str, found: list[str]) -> None:
    assert tokens([line], NAMES) == found


@pytest.mark.parametrize(
    ("line", "found"),
    [
        ("Example-Private", ["Example-Private"]),
        ("example-PRIVATE", ["example-PRIVATE"]),
        ("see Example-Lab", ["Example-Lab"]),
        ("example-private.git", ["example-private.git"]),
        ("git@host:o/example-private.git", ["example-private.git"]),
        ("clone example-lab.git now", ["example-lab.git"]),
        ("non-private semi-private", ["non-private", "semi-private"]),
        ("x--private _-private", ["x--private", "_-private"]),
    ],
)
def test_case_and_git_suffix_are_ignored(line: str, found: list[str]) -> None:
    assert tokens([line], NAMES) == found


def test_dotted_configured_name_matches() -> None:
    assert tokens(["see example.lab here"], ["example.lab"]) == ["example.lab"]


def test_multiple_per_line_and_repeats_collapse() -> None:
    line = "example-private and example-lab and example-private"
    assert tokens([line], NAMES) == ["example-private", "example-lab"]


def test_line_numbers_follow_first() -> None:
    lines = ["clean", "example-private", "clean", "example-lab"]
    assert [no for no, _ in pn.private_name_problems(lines, NAMES)] == [2, 4]
    found = pn.private_name_problems(lines, NAMES, 10)
    assert [no for no, _ in found] == [11, 13]


# The loader reads the user config and `origin`.


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    return tmp_path / "xdg" / "tbhbdev" / "config.toml"


def write_config(where: Path, text: str) -> None:
    where.parent.mkdir(parents=True, exist_ok=True)
    where.write_text(text)


def set_origin(monkeypatch: pytest.MonkeyPatch, code: int, url: str) -> None:

    def fake(*args: str, **kwargs: object) -> tuple[int, str]:
        del kwargs
        assert args == ("config", "--get", "remote.origin.url")
        return code, url

    monkeypatch.setattr(pn, "git", fake)


def test_unreadable_config_falls_back_to_the_suffix_rule(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    set_origin(monkeypatch, 0, "git@github.com:org/example-public.git")
    home.parent.mkdir(parents=True, exist_ok=True)
    home.write_bytes(b"\xff\xfe")
    assert pn.private_names() == []
    home.unlink()
    home.mkdir()
    assert pn.private_names() == []
    assert len(pr.check_text("# t\n\nexample-private\n").problems) >= 1


def test_no_config_gives_no_names(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    del home
    set_origin(monkeypatch, 0, "git@github.com:org/example-public.git")
    assert pn.private_names() == []


def test_missing_table_or_key_gives_no_names(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    set_origin(monkeypatch, 0, "git@github.com:org/example-public.git")
    write_config(home, "[harness]\nrule = 'x'\n")
    assert pn.private_names() == []
    write_config(home, "[pr_check]\n")
    assert pn.private_names() == []


def test_configured_names_are_returned(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    set_origin(monkeypatch, 0, "https://github.com/org/example-public")
    write_config(home, '[pr_check]\nprivate_repos = ["example-lab"]\n')
    assert pn.private_names() == ["example-lab"]


@pytest.mark.parametrize(
    "text",
    [
        "not toml [",
        "pr_check = 3\n",
        "[pr_check]\nprivate_repos = 'example-lab'\n",
        "[pr_check]\nprivate_repos = []\n",
        "[pr_check]\nprivate_repos = [1]\n",
    ],
)
def test_bad_config_falls_back_to_the_suffix_rule(
    home: Path, monkeypatch: pytest.MonkeyPatch, text: str
) -> None:
    set_origin(monkeypatch, 0, "git@github.com:org/example-public.git")
    write_config(home, text)
    assert pn.private_names() == []


@pytest.mark.parametrize(
    "url",
    [
        "git@github.com:org/example-private.git",
        "https://github.com/org/example-private/",
        "https://github.com/org/example-lab",
        "  git@github.com:org/example-lab.git\n",
        "git@github.com:org/Example-Private.git",
        "https://github.com/org/Example-LAB",
    ],
)
def test_private_origin_skips_the_check(
    home: Path, monkeypatch: pytest.MonkeyPatch, url: str
) -> None:
    set_origin(monkeypatch, 0, url)
    write_config(home, '[pr_check]\nprivate_repos = ["example-lab"]\n')
    assert pn.private_names() is None
    assert pn.draft_problems(["example-private"], 1) == []


def test_origin_name_keeps_a_trailing_capital_x(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    set_origin(monkeypatch, 0, "git@github.com:org/example-labX")
    write_config(home, '[pr_check]\nprivate_repos = ["example-lab"]\n')
    assert pn.private_names() == ["example-lab"]


@pytest.mark.parametrize(("code", "url"), [(1, ""), (128, "x"), (0, ""), (0, "/")])
def test_unreadable_origin_still_flags(
    home: Path, monkeypatch: pytest.MonkeyPatch, code: int, url: str
) -> None:
    del home
    set_origin(monkeypatch, code, url)
    assert pn.private_names() == []
    assert len(pn.draft_problems(["example-private"], 5)) == 1


def test_public_origin_flags(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    set_origin(monkeypatch, 0, "git@github.com:org/example-public.git")
    write_config(home, '[pr_check]\nprivate_repos = ["example-lab"]\n')
    found = pn.draft_problems(["# t", "", "example-lab"], 4)
    assert [no for no, _ in found] == [6]


def test_check_text_flags_title_and_body(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    set_origin(monkeypatch, 0, "git@github.com:org/example-public.git")
    write_config(home, '[pr_check]\nprivate_repos = ["example-lab"]\n')
    text = (
        "# fix: x for example-private\n\n## Why\n\nSee example-lab.\n\n"
        "## Behavior change\n\nb\n\n## Verification\n\nv\n"
    )
    found = pr.check_text(text).problems
    assert [(no, m.split("'")[1]) for no, m in found if "private repo" in m] == [
        (1, "example-private"),
        (5, "example-lab"),
    ]
    set_origin(monkeypatch, 0, "git@github.com:org/example-lab.git")
    assert not [m for _, m in pr.check_text(text).problems if "private repo" in m]

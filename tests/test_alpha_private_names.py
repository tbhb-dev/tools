"""Tests for the private repository name check in tbhbdev alpha pr check."""

from __future__ import annotations

from pathlib import Path

import pytest
from fake_tool import break_host_names

from tbhbdev.alpha import private_names as pn
from tbhbdev.alpha import pull_request as pr

NAMES = ["example-lab"]


def tokens(lines: list[str], names: list[str] | None = None) -> list[str]:
    found = pn.private_name_problems(lines, names or [])
    return [message.split("'")[1] for _, message in found]


def test_suffix_rule_flags_without_config() -> None:
    assert tokens(["see other-private for details"]) == ["other-private"]


def test_configured_name_is_flagged() -> None:
    assert tokens(["moved to example-lab today"], NAMES) == ["example-lab"]
    assert tokens(["moved to example-lab today"]) == []


def test_message_says_private_and_generic() -> None:
    [(_, message)] = pn.private_name_problems(["other-private"], [])
    assert message == (
        "'other-private' names a private repository; describe it generically instead"
    )


@pytest.mark.parametrize(
    "line",
    [
        "my-example-lab",
        "example-lab2",
        "example-labs",
        "example_example-lab",
        "example.example-lab",
        "other-privately",
        "--private",
        "-private",
        "the -private suffix",
        "other-private.git.x",
    ],
)
def test_longer_tokens_and_leading_hyphens_are_not_flagged(line: str) -> None:
    assert tokens([line], NAMES) == []


@pytest.mark.parametrize(
    ("line", "found"),
    [
        ("(other-private)", ["other-private"]),
        ("other-private.", ["other-private"]),
        ("`other-private`, x", ["other-private"]),
        ("tbhb-dev/other-private/x", ["other-private"]),
        ("example-lab.dev", []),
    ],
)
def test_punctuation_bounds_a_token(line: str, found: list[str]) -> None:
    assert tokens([line], NAMES) == found


@pytest.mark.parametrize(
    ("line", "found"),
    [
        ("Other-Private", ["Other-Private"]),
        ("other-PRIVATE", ["other-PRIVATE"]),
        ("see Example-Lab", ["Example-Lab"]),
        ("other-private.git", ["other-private.git"]),
        ("git@host:o/other-private.git", ["other-private.git"]),
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
    line = "other-private and example-lab and other-private"
    assert tokens([line], NAMES) == ["other-private", "example-lab"]


def test_line_numbers_follow_first() -> None:
    lines = ["clean", "other-private", "clean", "example-lab"]
    assert [no for no, _ in pn.private_name_problems(lines, NAMES)] == [2, 4]
    found = pn.private_name_problems(lines, NAMES, 10)
    assert [no for no, _ in found] == [11, 13]


# The loader reads the user config and `origin`.


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    path = Path.home() / ".local/state/tbhb/private-repos.txt"
    write_config(path, "example-host\n")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    return tmp_path / "xdg" / "tbhbdev" / "config.toml"


def write_config(where: Path, text: str) -> None:
    where.parent.mkdir(parents=True, exist_ok=True)
    where.write_text(text)


@pytest.fixture(autouse=True)
def no_lint(monkeypatch: pytest.MonkeyPatch) -> None:
    """The linters need vale and the like, which a sandbox may lack."""

    def fake(rendered: object, first: int) -> list[pr.Problem]:
        del rendered, first
        return []

    monkeypatch.setattr(pr, "lint", fake)


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
    assert pn.private_names() == pn.Names(["example-host"], set())
    home.unlink()
    home.mkdir()
    assert pn.private_names() == pn.Names(["example-host"], set())
    assert len(pr.check_text("# t\n\nother-private\n").problems) >= 1


def test_no_config_gives_no_names(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    del home
    set_origin(monkeypatch, 0, "git@github.com:org/example-public.git")
    assert pn.private_names() == pn.Names(["example-host"], set())


def test_missing_table_or_key_gives_no_names(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    set_origin(monkeypatch, 0, "git@github.com:org/example-public.git")
    write_config(home, "[harness]\nrule = 'x'\n")
    assert pn.private_names() == pn.Names(["example-host"], set())
    write_config(home, "[pr_check]\n")
    assert pn.private_names() == pn.Names(["example-host"], set())


def test_configured_names_are_returned(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    set_origin(monkeypatch, 0, "https://github.com/org/example-public")
    write_config(home, '[pr_check]\nprivate_repos = ["example-lab"]\n')
    assert pn.private_names() == pn.Names(["example-lab", "example-host"], set())


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
    assert pn.private_names() == pn.Names(["example-host"], set())


@pytest.mark.parametrize(
    "url",
    [
        "git@github.com:org/other-private.git",
        "https://github.com/org/other-private/",
        "https://github.com/org/example-lab",
        "  git@github.com:org/example-lab.git\n",
        "git@github.com:org/Other-Private.git",
        "https://github.com/org/Example-LAB",
    ],
)
def test_private_origin_skips_the_check(
    home: Path, monkeypatch: pytest.MonkeyPatch, url: str
) -> None:
    set_origin(monkeypatch, 0, url)
    write_config(home, '[pr_check]\nprivate_repos = ["example-lab"]\n')
    assert pn.private_names() is None
    assert pn.draft_problems(["other-private"], 1) == []


def test_origin_name_keeps_a_trailing_capital_x(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    set_origin(monkeypatch, 0, "git@github.com:org/example-labX")
    write_config(home, '[pr_check]\nprivate_repos = ["example-lab"]\n')
    assert pn.private_names() == pn.Names(["example-lab", "example-host"], set())


@pytest.mark.parametrize(("code", "url"), [(1, ""), (128, "x"), (0, ""), (0, "/")])
def test_unreadable_origin_still_flags(
    home: Path, monkeypatch: pytest.MonkeyPatch, code: int, url: str
) -> None:
    del home
    set_origin(monkeypatch, code, url)
    assert pn.private_names() == pn.Names(["example-host"], set())
    assert len(pn.draft_problems(["other-private"], 5)) == 1


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
        "# fix: x for other-private\n\n## Why\n\nSee example-lab.\n\n"
        "## Behavior change\n\nb\n\n## Verification\n\nv\n"
    )
    found = pr.check_text(text).problems
    assert [(no, m.split("'")[1]) for no, m in found if "private repo" in m] == [
        (1, "other-private"),
        (5, "example-lab"),
    ]
    set_origin(monkeypatch, 0, "git@github.com:org/example-lab.git")
    assert not [m for _, m in pr.check_text(text).problems if "private repo" in m]


@pytest.mark.parametrize(
    "text", ["example-private", "(example-private)", "`example-private`"]
)
def test_reviewed_placeholder(text: str) -> None:
    assert tokens([text], ["example-private"]) == []


@pytest.mark.parametrize(
    "text",
    [
        "other-private",
        "example-private-x",
        "Example-Private",
        "example-private.git",
    ],
)
def test_placeholder_exemption_is_exact(text: str) -> None:
    assert tokens([text]) == [text]


@pytest.mark.parametrize(
    "kind", ["missing", "directory", "empty", "comments", "invalid"]
)
def test_host_list_required(
    home: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    del home
    set_origin(monkeypatch, 0, "https://example.test/public")
    path = Path.home() / ".local/state/tbhb/private-repos.txt"
    break_host_names(path, kind)
    assert pn.draft_problems(["safe"], 7) == [
        (7, f"private names: cannot read a nonempty list from {path}")
    ]
    assert any(
        str(path) in message for _, message in pr.check_text("# title\n").problems
    )


def test_host_names_and_path_words(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    set_origin(monkeypatch, 0, "https://example.test/public")
    write_config(home, '[pr_check]\nprivate_repos = ["example-config"]\n')
    path = Path.home() / ".local/state/tbhb/private-repos.txt"
    path.write_text(
        "\n# ignored\n# Bare-word exempt, flagged only in path position: "
        "example sample\nexample-host\nexample\nsample\n"
    )
    lines = [
        "example-host example-config",
        "example SAMPLE",
        "org/EXAMPLE sample.GIT",
        "my-example.git org/example-more",
    ]
    found = pn.draft_problems(lines, 3)
    assert [(number, message.split("'")[1]) for number, message in found] == [
        (3, "example-host"),
        (3, "example-config"),
        (5, "EXAMPLE"),
        (5, "sample.GIT"),
    ]
    set_origin(monkeypatch, 0, "https://example.test/example-host")
    assert pn.draft_problems(lines, 3) == []
    set_origin(monkeypatch, 0, "https://example.test/example")
    assert pn.draft_problems(lines, 3) == []

    # Header entries also apply when the body doesn't repeat them.
    set_origin(monkeypatch, 0, "https://example.test/public")
    path.write_text(
        "# Bare-word exempt, flagged only in path position: EXAMPLE Sample\n"
        "example-host\n"
    )
    found = pn.draft_problems(["EXAMPLE sample", "org/EXAMPLE sample.GIT"], 3)
    assert [(number, message.split("'")[1]) for number, message in found] == [
        (4, "EXAMPLE"),
        (4, "sample.GIT"),
    ]


@pytest.mark.parametrize(("prefix", "suffix"), [("x", ""), ("", "-x")])
def test_placeholder_boundaries(prefix: str, suffix: str) -> None:
    text = f"{prefix}example-private{suffix}"
    assert tokens([text]) == [text]


@pytest.mark.parametrize("url", ["other-private", "https://[bad/other-private"])
def test_malformed_origin_is_public(
    home: Path, monkeypatch: pytest.MonkeyPatch, url: str
) -> None:
    del home
    set_origin(monkeypatch, 0, url)
    assert pn.private_names() == pn.Names(["example-host"], set())


def test_host_rules_preserve_names_and_normalize_header(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    del home
    set_origin(monkeypatch, 0, "https://example.test/public")
    path = Path.home() / ".local/state/tbhb/private-repos.txt"
    path.write_text(
        "  \n  # comment\n  # Bare-word exempt, flagged only in path position: "
        "EXAMPLE Sample\n  Example-Host  \n"
    )
    assert pn.private_names() == pn.Names(["Example-Host"], {"example", "sample"})
    set_origin(monkeypatch, 0, "https://example.test/example-host")
    assert pn.private_names() is None
    set_origin(monkeypatch, 0, "https://example.test/example")
    assert pn.private_names() is None


def test_header_restricts_suffix_names_too(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    del home
    set_origin(monkeypatch, 0, "https://example.test/public")
    path = Path.home() / ".local/state/tbhb/private-repos.txt"
    path.write_text(
        "# Bare-word exempt, flagged only in path position: other-private\n"
        "example-host\nother-private\n"
    )
    found = pn.draft_problems(
        ["other-private", "org/other-private", "OTHER-PRIVATE.git"], 1
    )
    assert [(line, message.split("'")[1]) for line, message in found] == [
        (2, "other-private"),
        (3, "OTHER-PRIVATE.git"),
    ]

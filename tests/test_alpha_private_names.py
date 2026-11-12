"""Tests for the private repository name check in tbhbdev alpha pr check."""

from __future__ import annotations

from pathlib import Path

import pytest
from fake_tool import PRIVATE_REPO, break_host_names, write_host_names

from tbhbdev.alpha import private_names as pn
from tbhbdev.alpha import pull_request as pr

NAMES = ["example-lab", PRIVATE_REPO]


def tokens(lines: list[str], names: list[str] | None = None) -> list[str]:
    found = pn.private_name_problems(lines, names or [])
    return [message.split("'")[1] for _, message in found]


def test_suffix_rule_flags_without_config(home: Path) -> None:
    del home
    name = "other-private"
    assert tokens([name]) == [name]
    assert tokens([pn.SUFFIX]) == []
    assert pn.names_for_repository(name) is None


def test_configured_name_is_flagged() -> None:
    assert tokens(["moved to example-lab today"], NAMES) == ["example-lab"]
    assert tokens(["moved to example-lab today"]) == []


def test_message_says_private_and_generic() -> None:
    [(_, message)] = pn.private_name_problems([PRIVATE_REPO], NAMES)
    assert message == (
        f"'{PRIVATE_REPO}' names a private repository; describe it generically instead"
    )


@pytest.mark.parametrize(
    "line",
    [
        "my-example-lab",
        "example-lab2",
        "example-labs",
        "example_example-lab",
        "example.example-lab",
        f"{PRIVATE_REPO}ly",
        f"--{PRIVATE_REPO}",
        f"-{PRIVATE_REPO}",
        f"the -{PRIVATE_REPO} prefix",
        f"{PRIVATE_REPO}.git.x",
    ],
)
def test_longer_tokens_and_leading_hyphens_are_not_flagged(line: str) -> None:
    assert tokens([line], NAMES) == []


@pytest.mark.parametrize(
    ("line", "found"),
    [
        (f"({PRIVATE_REPO})", [PRIVATE_REPO]),
        (f"{PRIVATE_REPO}.", [PRIVATE_REPO]),
        (f"`{PRIVATE_REPO}`, x", [PRIVATE_REPO]),
        (f"tbhb-dev/{PRIVATE_REPO}/x", [PRIVATE_REPO]),
        ("example-lab.dev", []),
    ],
)
def test_punctuation_bounds_a_token(line: str, found: list[str]) -> None:
    assert tokens([line], NAMES) == found


@pytest.mark.parametrize(
    ("line", "found"),
    [
        (PRIVATE_REPO.capitalize(), [PRIVATE_REPO.capitalize()]),
        (PRIVATE_REPO.upper(), [PRIVATE_REPO.upper()]),
        ("see Example-Lab", ["Example-Lab"]),
        (f"{PRIVATE_REPO}.git", [f"{PRIVATE_REPO}.git"]),
        (f"git@host:o/{PRIVATE_REPO}.git", [f"{PRIVATE_REPO}.git"]),
        ("clone example-lab.git now", ["example-lab.git"]),
        (f"{PRIVATE_REPO} example-lab", [PRIVATE_REPO, "example-lab"]),
    ],
)
def test_case_and_git_suffix_are_ignored(line: str, found: list[str]) -> None:
    assert tokens([line], NAMES) == found


@pytest.mark.parametrize("prefix", ["x--", "_-"])
def test_configured_names_keep_hyphens_and_underscores(prefix: str) -> None:
    name = f"{prefix}{PRIVATE_REPO}"
    assert tokens([name], [name]) == [name]


def test_dotted_configured_name_matches() -> None:
    assert tokens(["see example.lab here"], ["example.lab"]) == ["example.lab"]


def test_multiple_per_line_and_repeats_collapse() -> None:
    line = f"{PRIVATE_REPO} and example-lab and {PRIVATE_REPO}"
    assert tokens([line], NAMES) == [PRIVATE_REPO, "example-lab"]


def test_line_numbers_follow_first() -> None:
    lines = ["clean", PRIVATE_REPO, "clean", "example-lab"]
    assert [no for no, _ in pn.private_name_problems(lines, NAMES)] == [2, 4]
    found = pn.private_name_problems(lines, NAMES, 10)
    assert [no for no, _ in found] == [11, 13]


# The loader reads the user config and `origin`.


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    write_host_names(Path.home())
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


def test_unreadable_config_keeps_host_names(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    set_origin(monkeypatch, 0, "git@github.com:org/example-public.git")
    home.parent.mkdir(parents=True, exist_ok=True)
    home.write_bytes(b"\xff\xfe")
    assert pn.private_names() == pn.Names(["example-host", PRIVATE_REPO], set())
    home.unlink()
    home.mkdir()
    assert pn.private_names() == pn.Names(["example-host", PRIVATE_REPO], set())
    assert len(pr.check_text(f"# t\n\n{PRIVATE_REPO}\n").problems) >= 1


def test_no_config_gives_no_names(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    del home
    set_origin(monkeypatch, 0, "git@github.com:org/example-public.git")
    assert pn.private_names() == pn.Names(["example-host", PRIVATE_REPO], set())


def test_missing_table_or_key_gives_no_names(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    set_origin(monkeypatch, 0, "git@github.com:org/example-public.git")
    write_config(home, "[harness]\nrule = 'x'\n")
    assert pn.private_names() == pn.Names(["example-host", PRIVATE_REPO], set())
    write_config(home, "[pr_check]\n")
    assert pn.private_names() == pn.Names(["example-host", PRIVATE_REPO], set())


def test_configured_names_are_returned(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    set_origin(monkeypatch, 0, "https://github.com/org/example-public")
    write_config(home, '[pr_check]\nprivate_repos = ["example-lab"]\n')
    assert pn.private_names() == pn.Names(
        ["example-lab", "example-host", PRIVATE_REPO], set()
    )


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
def test_bad_config_keeps_host_names(
    home: Path, monkeypatch: pytest.MonkeyPatch, text: str
) -> None:
    set_origin(monkeypatch, 0, "git@github.com:org/example-public.git")
    write_config(home, text)
    assert pn.private_names() == pn.Names(["example-host", PRIVATE_REPO], set())


@pytest.mark.parametrize(
    "url",
    [
        f"git@github.com:org/{PRIVATE_REPO}.git",
        f"https://github.com/org/{PRIVATE_REPO}/",
        "https://github.com/org/example-lab",
        "  git@github.com:org/example-lab.git\n",
        f"git@github.com:org/{PRIVATE_REPO.capitalize()}.git",
        "https://github.com/org/Example-LAB",
    ],
)
def test_private_origin_skips_the_check(
    home: Path, monkeypatch: pytest.MonkeyPatch, url: str
) -> None:
    set_origin(monkeypatch, 0, url)
    write_config(home, '[pr_check]\nprivate_repos = ["example-lab"]\n')
    assert pn.private_names() is None
    assert pn.draft_problems([PRIVATE_REPO], 1) == []


def test_origin_name_keeps_a_trailing_capital_x(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    set_origin(monkeypatch, 0, "git@github.com:org/example-labX")
    write_config(home, '[pr_check]\nprivate_repos = ["example-lab"]\n')
    assert pn.private_names() == pn.Names(
        ["example-lab", "example-host", PRIVATE_REPO], set()
    )


@pytest.mark.parametrize(("code", "url"), [(1, ""), (128, "x"), (0, ""), (0, "/")])
def test_unreadable_origin_still_flags(
    home: Path, monkeypatch: pytest.MonkeyPatch, code: int, url: str
) -> None:
    del home
    set_origin(monkeypatch, code, url)
    assert pn.private_names() == pn.Names(["example-host", PRIVATE_REPO], set())
    assert len(pn.draft_problems([PRIVATE_REPO], 5)) == 1


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
        f"# fix: x for {PRIVATE_REPO}\n\n## Why\n\nSee example-lab.\n\n"
        "## Behavior change\n\nb\n\n## Verification\n\nv\n"
    )
    found = pr.check_text(text).problems
    assert [(no, m.split("'")[1]) for no, m in found if "private repo" in m] == [
        (1, PRIVATE_REPO),
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
        PRIVATE_REPO,
        f"{PRIVATE_REPO}-x",
        PRIVATE_REPO.upper(),
        f"{PRIVATE_REPO}.git",
    ],
)
def test_configured_variants_are_flagged(text: str) -> None:
    assert tokens([text], [PRIVATE_REPO, f"{PRIVATE_REPO}-x"]) == [text]


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
def test_configured_name_boundaries(prefix: str, suffix: str) -> None:
    text = f"{prefix}{PRIVATE_REPO}{suffix}"
    assert tokens([text], NAMES) == []
    assert tokens([text], [text]) == [text]


@pytest.mark.parametrize("url", [PRIVATE_REPO, f"https://[bad/{PRIVATE_REPO}"])
def test_malformed_origin_is_public(
    home: Path, monkeypatch: pytest.MonkeyPatch, url: str
) -> None:
    del home
    set_origin(monkeypatch, 0, url)
    assert pn.private_names() == pn.Names(["example-host", PRIVATE_REPO], set())


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


def test_header_restricts_configured_names_too(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    del home
    set_origin(monkeypatch, 0, "https://example.test/public")
    path = Path.home() / ".local/state/tbhb/private-repos.txt"
    path.write_text(
        f"# Bare-word exempt, flagged only in path position: {PRIVATE_REPO}\n"
        f"example-host\n{PRIVATE_REPO}\n"
    )
    found = pn.draft_problems(
        [PRIVATE_REPO, f"org/{PRIVATE_REPO}", f"{PRIVATE_REPO.upper()}.git"], 1
    )
    assert [(line, message.split("'")[1]) for line, message in found] == [
        (2, PRIVATE_REPO),
        (3, f"{PRIVATE_REPO.upper()}.git"),
    ]


def test_parse_list_splits_names_and_path_only_words() -> None:
    lines = ["# note", "", f"{pn.PATH_HEADER} Alpha beta", "  example-one  "]
    assert pn.parse_list(lines) == pn.Names(["example-one"], {"alpha", "beta"})


@pytest.mark.parametrize(
    ("lines", "expected"),
    [
        (["+a", "+b"], []),
        (['+x = "ab"', '+"cd"'], [(1, '+x = "abcd"')]),
        (['+"ab",', '+  r"cd"'], [(1, '+"abcd"')]),
        (['+"ab" +', "+  'cd'", '+"ef"'], [(1, '+"abcdef"')]),
        (['+"ab"', "+x = 1", '+"cd"'], []),
        (['+"ab"', ' "cd"'], []),
        (['-"ab"', '+"cd"'], []),
    ],
)
def test_joined_literals_merge_only_added_adjacent_literals(
    lines: list[str], expected: list[tuple[int, str]]
) -> None:
    assert pn.joined_literals(lines) == expected

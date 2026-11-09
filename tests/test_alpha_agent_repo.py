"""Repo resolution is a table over literal arguments, with no file touched."""

from __future__ import annotations

from pathlib import PurePosixPath

import pytest

from tbhbdev.alpha.agent.decisions import repo
from tbhbdev.alpha.agent.model import Invalid

HOME = PurePosixPath("/example-home")


@pytest.mark.parametrize(
    ("argument", "expected"),
    [
        ("example-org/example-repo", ("github.com", "example-org", "example-repo")),
        ("a/b", ("github.com", "a", "b")),
        ("example-org/.github", ("github.com", "example-org", ".github")),
        ("example-org/a_b.c-d", ("github.com", "example-org", "a_b.c-d")),
        ("example-org/..x", ("github.com", "example-org", "..x")),
        ("a-b-c/r", ("github.com", "a-b-c", "r")),
        ("A1/R2", ("github.com", "A1", "R2")),
        ("git.example.com/example-org/r", ("git.example.com", "example-org", "r")),
        ("example.com/o/r", ("example.com", "o", "r")),
        ("x/o/r", ("x", "o", "r")),
        ("a-b.c/o/r", ("a-b.c", "o", "r")),
        ("9x/o/r", ("9x", "o", "r")),
        (f"{'a' * 39}/r", ("github.com", "a" * 39, "r")),
        (f"o/{'r' * 100}", ("github.com", "o", "r" * 100)),
        (f"{'a' * 63}.example/o/r", (f"{'a' * 63}.example", "o", "r")),
    ],
)
def test_parts_accepts_both_forms(argument: str, expected: tuple[str, ...]) -> None:
    assert repo.parts(argument) == expected


@pytest.mark.parametrize(
    ("argument", "why"),
    [
        ("/o/r", "must not be an absolute path"),
        ("/", "must not be an absolute path"),
        ("", "has an empty part"),
        ("o//r", "has an empty part"),
        ("o/r/", "has an empty part"),
        ("o/", "has an empty part"),
        ("../r", "has a '..' part"),
        ("o/..", "has a '..' part"),
        ("github.com/../r", "has a '..' part"),
        ("r", "must be org/repo or <git host>/<org>/<repo>"),
        ("a/b/c/d", "must be org/repo or <git host>/<org>/<repo>"),
        ("./r", "has an invalid owner '.'"),
        ("o/.", "has an invalid name '.'"),
        ("-o/r", "has an invalid owner '-o'"),
        ("o-/r", "has an invalid owner 'o-'"),
        ("o--p/r", "has an invalid owner 'o--p'"),
        ("o_p/r", "has an invalid owner 'o_p'"),
        ("o.p/r", "has an invalid owner 'o.p'"),
        (f"{'a' * 40}/r", f"has an invalid owner '{'a' * 40}'"),
        ("o/r s", "has an invalid name 'r s'"),
        ("o/r\n", "has an invalid name 'r\\n'"),
        ("o/r\x00", "has an invalid name 'r\\x00'"),
        ("o/r$", "has an invalid name 'r$'"),
        ("o/é", "has an invalid name 'é'"),
        ("o\\r/x", "has an invalid owner 'o\\\\r'"),
        (f"o/{'r' * 101}", f"has an invalid name '{'r' * 101}'"),
        ("-x/o/r", "has an invalid git host '-x'"),
        ("x-/o/r", "has an invalid git host 'x-'"),
        ("x_y/o/r", "has an invalid git host 'x_y'"),
        ("a:b/o/r", "has an invalid git host 'a:b'"),
        (f"{'a' * 64}.com/o/r", f"has an invalid git host '{'a' * 64}.com'"),
        (f"{'a.' * 126}ab/o/r", f"has an invalid git host '{'a.' * 126}ab'"),
    ],
)
def test_parts_refuses(argument: str, why: str) -> None:
    with pytest.raises(Invalid) as caught:
        repo.parts(argument)
    assert str(caught.value) == f"repo {argument!r} {why}"


def test_parts_accepts_a_host_of_253_characters() -> None:
    host = ".".join(["a" * 61] * 4 + ["b" * 5])
    assert len(host) == 253
    assert repo.parts(f"{host}/o/r") == (host, "o", "r")


def test_clone_path_is_under_home() -> None:
    assert repo.clone_path(HOME, "example-org/example-repo") == PurePosixPath(
        "/example-home/Code/github.com/example-org/example-repo"
    )
    assert repo.clone_path(HOME, "git.example.com/o/r") == PurePosixPath(
        "/example-home/Code/git.example.com/o/r"
    )


def test_clone_path_refuses_what_parts_refuses() -> None:
    with pytest.raises(Invalid):
        repo.clone_path(HOME, "../r")

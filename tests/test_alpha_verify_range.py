"""Real commit objects exercise the range verifier without signing keys or hooks."""

import subprocess
from typing import TYPE_CHECKING, TypedDict, Unpack

import pytest

from tbhbdev import alpha
from tbhbdev.alpha import identity, verify_range

if TYPE_CHECKING:
    from pathlib import Path


class Change(TypedDict, total=False):
    author: str
    committer: str
    signed: bool
    message: str


class RunOptions(TypedDict):
    check: bool
    capture_output: bool
    text: bool


PROG = "tbhbdev alpha verify-range"
TRAILER = "Generated-by: LLM harness=codex;model=test"
MESSAGE = f"feat: example\n\n{TRAILER}\n"
EMAIL = identity.ident_email(identity.AGENT_IDENT)


class Repository:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.git("init", "-q")
        self.tree = self.git("hash-object", "-t", "tree", "-w", "--stdin", data="")
        self.base = self.commit(parents=[])

    def git(self, *args: str, data: str | None = None) -> str:
        return subprocess.run(
            ["git", "-C", str(self.path), *args],
            input=data,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()

    def commit(
        self,
        *,
        author: str = EMAIL,
        committer: str = EMAIL,
        signed: bool = True,
        message: str = MESSAGE,
        parents: list[str] | None = None,
    ) -> str:
        links = [self.base] if parents is None else parents
        headers = [f"tree {self.tree}", *(f"parent {p}" for p in links)]
        headers.extend(
            f"{role} Example <{address}> 1700000000 +0000"
            for role, address in (("author", author), ("committer", committer))
        )
        if signed:
            headers.extend(["gpgsig fake signature", " continued signature"])
        return self.git(
            "hash-object",
            "-t",
            "commit",
            "-w",
            "--stdin",
            data="\n".join(headers) + "\n\n" + message,
        )


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Repository:
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.chdir(tmp_path)
    return Repository(tmp_path)


def test_valid_range_and_dispatch(
    repo: Repository, capsys: pytest.CaptureFixture[str]
) -> None:
    first = repo.commit()
    second = repo.commit(parents=[first], message=f"fix: second\n\n{TRAILER}\n")
    repo.git("update-ref", "refs/heads/test", second)
    assert alpha.main(["verify-range", repo.base, "test"], "tbhbdev alpha") == 0
    assert capsys.readouterr() == (
        (
            f"{first[:7]} ok feat: example\n{second[:7]} ok fix: second\n"
            "verify-range: 2 commits, 0 failed\n"
        ),
        "",
    )
    assert alpha.COMMANDS["verify-range"] is verify_range.main
    assert (
        "  verify-range BASE TIP [-C PATH]: check every agent commit in a range\n"
        in alpha.USAGE
    )


@pytest.mark.parametrize(
    ("change", "reasons"),
    [
        (
            {"author": "human@example.com"},
            [f"author email is 'human@example.com'; expected {EMAIL}"],
        ),
        (
            {"committer": "human@example.com"},
            [f"committer email is 'human@example.com'; expected {EMAIL}"],
        ),
        ({"signed": False}, ["missing gpgsig header"]),
        ({"message": "feat: absent\n"}, ["missing Generated-by trailer with model="]),
        (
            {"message": "feat: missing model\n\nGenerated-by: LLM harness=codex\n"},
            [
                "Generated-by: LLM harness=codex: model required",
                "missing Generated-by trailer with model=",
            ],
        ),
        (
            {"message": "feat: invalid\n\nGenerated-by: person\n"},
            [
                "Generated-by: person: must start with literal LLM",
                "missing Generated-by trailer with model=",
            ],
        ),
        (
            {"message": MESSAGE + "Assisted-by: LLM harness=unknown;model=test\n"},
            [
                "Assisted-by: LLM harness=unknown;model=test: unknown harness unknown",
            ],
        ),
        (
            {
                "message": "feat: reversed\n\n"
                "Assisted-by: LLM harness=codex;model=test\n" + TRAILER + "\n"
            },
            [
                TRAILER + ": Generated-by must come before Assisted-by",
            ],
        ),
        (
            {"message": MESSAGE + "sIgNeD-oFf-bY \t: Example <human@example.com>\n"},
            [identity.SIGNOFF_ERROR],
        ),
        (
            {"message": MESSAGE + "Co-authored-by: Example <human@example.com>\n"},
            [identity.SIGNOFF_ERROR],
        ),
        (
            {"author": "123+bot@users.noreply.github.com"},
            [
                f"author email is '123+bot@users.noreply.github.com'; expected {EMAIL}",
                "author uses a GitHub noreply address",
            ],
        ),
        (
            {"committer": "NOREPLY@GITHUB.COM"},
            [
                f"committer email is 'NOREPLY@GITHUB.COM'; expected {EMAIL}",
                "committer uses a GitHub noreply address",
            ],
        ),
        (
            {"author": "BOT@USERS.NOREPLY.GITHUB.COM"},
            [
                f"author email is 'BOT@USERS.NOREPLY.GITHUB.COM'; expected {EMAIL}",
                "author uses a GitHub noreply address",
            ],
        ),
        (
            {"committer": "bot@users.noreply.github.com"},
            [
                f"committer email is 'bot@users.noreply.github.com'; expected {EMAIL}",
                "committer uses a GitHub noreply address",
            ],
        ),
        (
            {"author": "noreply@github.com"},
            [
                f"author email is 'noreply@github.com'; expected {EMAIL}",
                "author uses a GitHub noreply address",
            ],
        ),
    ],
)
def test_failed_rules(
    repo: Repository,
    capsys: pytest.CaptureFixture[str],
    change: Change,
    reasons: list[str],
) -> None:
    tip = repo.commit(**change)
    subject = str(change.get("message", MESSAGE)).splitlines()[0]
    assert verify_range.main([repo.base, tip], PROG) == 1
    assert capsys.readouterr() == (
        (
            f"{tip[:7]} FAIL {'; '.join(reasons)} | {subject}\n"
            "verify-range: 1 commits, 1 failed\n"
        ),
        "",
    )


def test_merge_and_multiple_failures(
    repo: Repository, capsys: pytest.CaptureFixture[str]
) -> None:
    first = repo.commit(signed=False)
    other = repo.commit(message=f"feat: branch\n\n{TRAILER}\n")
    tip = repo.commit(parents=[first, other], author="other@example.com", signed=False)
    assert verify_range.main([repo.base, tip], PROG) == 1
    out, err = capsys.readouterr()
    assert err == ""
    assert out == (
        f"{first[:7]} FAIL missing gpgsig header | feat: example\n"
        f"{other[:7]} ok feat: branch\n"
        f"{tip[:7]} FAIL author email is 'other@example.com'; expected {EMAIL}; "
        "missing gpgsig header; expected one parent, found 2 | feat: example\n"
        "verify-range: 3 commits, 2 failed\n"
    )


def test_root_is_not_one_parent(repo: Repository) -> None:
    assert verify_range.inspect_commit(repo.git("cat-file", "commit", repo.base)) == (
        ["expected one parent, found 0"],
        "feat: example",
    )


def test_empty_range(repo: Repository, capsys: pytest.CaptureFixture[str]) -> None:
    assert verify_range.main([repo.base, repo.base], PROG) == 0
    assert capsys.readouterr() == ("verify-range: 0 commits, 0 failed\n", "")


@pytest.mark.parametrize(
    "refs", [("missing", "base"), ("base", "missing"), ("--all", "base")]
)
def test_bad_ref(
    repo: Repository, capsys: pytest.CaptureFixture[str], refs: tuple[str, str]
) -> None:
    args = [repo.base if ref == "base" else ref for ref in refs]
    assert verify_range.main(["--", *args], PROG) == 2
    out, err = capsys.readouterr()
    assert out == ""
    assert err.startswith(PROG + ": Command [")
    assert "rev-parse" in err


def test_path_with_changed_tree(
    repo: Repository,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    blob = repo.git("hash-object", "-w", "--stdin", data="example\n")
    repo.tree = repo.git("mktree", data=f"100644 blob {blob}\tfile\n")
    tip = repo.commit()
    monkeypatch.chdir(repo.path.parent)
    assert verify_range.main([repo.base, tip, "-C", str(repo.path)], PROG) == 0
    assert capsys.readouterr() == (
        (f"{tip[:7]} ok feat: example\nverify-range: 1 commits, 0 failed\n"),
        "",
    )


@pytest.mark.parametrize(
    "argv", [[], ["base"], ["base", "tip", "extra"], ["base", "tip", "-C"]]
)
def test_usage(argv: list[str], capsys: pytest.CaptureFixture[str]) -> None:
    assert verify_range.main(argv, "custom") == 2
    out, err = capsys.readouterr()
    assert out == ""
    assert err.startswith("usage: custom [-h] [-C PATH] BASE TIP\n")


@pytest.mark.parametrize("flag", ["-h", "--help"])
def test_help(flag: str, capsys: pytest.CaptureFixture[str]) -> None:
    assert verify_range.main([flag], "custom") == 0
    out, err = capsys.readouterr()
    assert err == ""
    assert out.startswith("usage: custom [-h] [-C PATH] BASE TIP\n")
    assert verify_range.__doc__ in out


def test_terminal_filter(repo: Repository, capsys: pytest.CaptureFixture[str]) -> None:
    tip = repo.commit(
        message="feat: unsafe\x1b[31m\u202e\ncontinued\n\n"
        "Generated-by: LLM harness=codex;model=test;unknown=\x1b[0m\n"
    )
    assert verify_range.main([repo.base, tip], PROG) == 1
    assert capsys.readouterr() == (
        (
            f"{tip[:7]} FAIL Generated-by: LLM harness=codex;model=test;unknown=[0m: "
            "unknown attribute unknown | feat: unsafe[31m continued\n"
            "verify-range: 1 commits, 1 failed\n"
        ),
        "",
    )


def test_signature_in_message_does_not_count(
    repo: Repository, capsys: pytest.CaptureFixture[str]
) -> None:
    tip = repo.commit(signed=False, message=MESSAGE + "gpgsig fake\n")
    assert verify_range.main([repo.base, tip], PROG) == 1
    assert "FAIL missing gpgsig header |" in capsys.readouterr().out


def test_missing_headers() -> None:
    assert verify_range.inspect_commit("tree ignored\n\n") == (
        [
            f"author email is ''; expected {EMAIL}",
            f"committer email is ''; expected {EMAIL}",
            "missing gpgsig header",
            "missing Generated-by trailer with model=",
            "expected one parent, found 0",
        ],
        "",
    )


def test_git_invocation(repo: Repository, monkeypatch: pytest.MonkeyPatch) -> None:
    run = subprocess.run
    calls: list[tuple[list[str], dict[str, object]]] = []

    def capture(
        args: list[str], **kwargs: Unpack[RunOptions]
    ) -> subprocess.CompletedProcess[bytes]:
        calls.append((args, dict(kwargs)))
        return run(args, **kwargs)

    monkeypatch.setattr(subprocess, "run", capture)
    assert verify_range.git_output(
        str(repo.path), "cat-file", "commit", repo.base
    ).startswith("tree ")
    assert calls == [
        (
            [
                "git",
                "--no-replace-objects",
                "-c",
                "core.fsmonitor=false",
                "-c",
                "core.pager=cat",
                "-C",
                str(repo.path),
                "cat-file",
                "commit",
                repo.base,
            ],
            {
                "check": True,
                "capture_output": True,
                "text": False,
            },
        )
    ]


def test_git_failure(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def unavailable(*_args: str) -> str:
        msg = "unavailable\x1b\u202e"
        raise OSError(msg)

    monkeypatch.setattr(verify_range, "git_output", unavailable)
    assert verify_range.main(["base", "tip"], "custom") == 2
    assert capsys.readouterr() == ("", "custom: unavailable\n")


@pytest.mark.parametrize(
    ("message", "subject"),
    [
        (
            "\n\nfeat: subject\ncontinued\n\nbody\n\n" + TRAILER,
            "feat: subject continued",
        ),
        ("  feat: indented\n\n" + TRAILER, "  feat: indented"),
        ("XXX: subject\n\n" + TRAILER, "XXX: subject"),
    ],
)
def test_subject_paragraph(message: str, subject: str, repo: Repository) -> None:
    tip = repo.commit(message=message)
    assert verify_range.inspect_commit(repo.git("cat-file", "commit", tip)) == (
        [],
        subject,
    )


def test_missing_identities_delegate_empty_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parse_email = identity.ident_email
    values: list[str] = []

    def capture(value: str) -> str:
        values.append(value)
        return parse_email(value)

    monkeypatch.setattr(identity, "ident_email", capture)
    verify_range.inspect_commit("tree ignored\n\n")
    assert values == ["", "", identity.AGENT_IDENT]


def test_range_git_arguments(repo: Repository, monkeypatch: pytest.MonkeyPatch) -> None:
    read_git = verify_range.git_output
    calls: list[tuple[str, ...]] = []
    tip = repo.commit()

    def capture(*args: str) -> str:
        calls.append(args)
        return read_git(*args)

    monkeypatch.setattr(verify_range, "git_output", capture)
    assert verify_range.main([repo.base, tip], PROG) == 0
    assert calls == [
        (".", "rev-parse", "--verify", "--end-of-options", repo.base + "^{commit}"),
        (".", "rev-parse", "--verify", "--end-of-options", tip + "^{commit}"),
        (".", "rev-list", "--reverse", "--topo-order", f"{repo.base}..{tip}", "--"),
        (".", "cat-file", "commit", tip),
    ]


@pytest.mark.parametrize(
    "separator",
    ["\r", "\x0b", "\x0c", "\x1c", "\x1d", "\x1e", "\x85", "\u2028", "\u2029"],
)
@pytest.mark.parametrize("header", ["author", "gpgsig", "parent"])
def test_name_cannot_inject_headers(
    repo: Repository,
    capsys: pytest.CaptureFixture[str],
    separator: str,
    header: str,
) -> None:
    author = "human@example.com" if header == "author" else EMAIL
    injected = {
        "author": f"author A <{EMAIL}> 1 +0000",
        "gpgsig": "gpgsig x",
        "parent": f"parent {repo.base}",
    }[header]
    headers = [
        f"tree {repo.tree}",
        f"parent {repo.base}",
        f"author Example <{author}> 1 +0000",
        f"committer Example{separator}{injected} <{EMAIL}> 1 +0000",
    ]
    if header != "gpgsig":
        headers.append("gpgsig fake")
    tip = repo.git(
        "hash-object",
        "-t",
        "commit",
        "-w",
        "--literally",
        "--stdin",
        data="\n".join(headers) + "\n\n" + MESSAGE,
    )
    expected = {
        "author": f"FAIL author email is 'human@example.com'; expected {EMAIL} |",
        "gpgsig": "FAIL missing gpgsig header |",
        "parent": "ok",
    }[header]
    assert verify_range.main([repo.base, tip], PROG) == int(header != "parent")
    assert capsys.readouterr() == (
        (
            f"{tip[:7]} {expected} feat: example\n"
            f"verify-range: 1 commits, {int(header != 'parent')} failed\n"
        ),
        "",
    )


@pytest.mark.parametrize("role", ["author", "committer"])
@pytest.mark.parametrize("prefix", ["", " ", "  "])
def test_first_identity_header_wins(repo: Repository, role: str, prefix: str) -> None:
    text = verify_range.git_output(str(repo.path), "cat-file", "commit", repo.commit())
    headers, _, message = text.partition("\n\n")
    text = headers + f"\n{prefix}{role} Other <human@example.com> 1 +0000\n\n" + message
    assert verify_range.inspect_commit(text) == ([], "feat: example")


@pytest.mark.parametrize("header", ["gpgsig", "gpgsig-sha256"])
def test_signature_headers(repo: Repository, header: str) -> None:
    text = verify_range.git_output(str(repo.path), "cat-file", "commit", repo.commit())
    assert verify_range.inspect_commit(text.replace("gpgsig", header)) == (
        [],
        "feat: example",
    )


@pytest.mark.parametrize("header", ["gpgsig", "gpgsig-sha256", "parent"])
def test_continuation_is_not_a_header(repo: Repository, header: str) -> None:
    text = verify_range.git_output(
        str(repo.path), "cat-file", "commit", repo.commit(signed=False)
    )
    text = text.replace("\n\n", f"\n {header} fake\n\n", 1)
    assert verify_range.inspect_commit(text) == (
        ["missing gpgsig header"],
        "feat: example",
    )


def test_replace_does_not_hide_original(
    repo: Repository, capsys: pytest.CaptureFixture[str]
) -> None:
    original = repo.commit(author="human@example.com", signed=False)
    replacement = repo.commit()
    repo.git("replace", original, replacement)
    assert verify_range.main([repo.base, original], PROG) == 1
    assert capsys.readouterr() == (
        (
            f"{original[:7]} FAIL author email is 'human@example.com'; "
            f"expected {EMAIL}; "
            "missing gpgsig header | feat: example\nverify-range: 1 commits, 1 failed\n"
        ),
        "",
    )


def test_git_decodes_bytes_without_newline_translation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def output(
        args: list[str], **_kwargs: Unpack[RunOptions]
    ) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(args, 0, stdout=b"name\rvalue\xff\n")

    monkeypatch.setattr(subprocess, "run", output)
    assert (
        verify_range.git_output(".", "cat-file", "commit", "HEAD")
        == "name\rvalue\ufffd\n"
    )

"""Push checks against disposable repositories and a bare remote."""

from __future__ import annotations

import io
import os
import subprocess
import sys
from pathlib import Path

import pytest
from fake_tool import PRIVATE_REPO as PRIVATE
from fake_tool import break_host_names, pythonpath, write_host_names

from tbhbdev import alpha
from tbhbdev.alpha import install
from tbhbdev.alpha import pre_push_guard as pg

ZERO = "0" * 40
PUBLIC = "https://github.com/example/public.git"


def git(path: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *pg.SAFE, *args],
        cwd=path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for name in [key for key in os.environ if key.startswith("GIT_")]:
        monkeypatch.delenv(name, raising=False)
    for name in (
        "HOME",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "XDG_STATE_HOME",
        "XDG_CACHE_HOME",
        "XDG_RUNTIME_DIR",
    ):
        monkeypatch.setenv(name, str(tmp_path / name.lower()))
    write_host_names(Path.home())
    for name in ("SKIP", "PREK_SKIP"):
        monkeypatch.delenv(name, raising=False)
    for role in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{role}_NAME", "Example")
        monkeypatch.setenv(f"GIT_{role}_EMAIL", "example@example.test")
    remote = tmp_path / "remote.git"
    remote.mkdir()
    git(remote, "init", "--bare", "-q")
    path = tmp_path / "repo"
    path.mkdir()
    git(path, "init", "-q")
    git(path, "config", "commit.gpgsign", "false")
    git(path, "remote", "add", "origin", str(remote))
    git(path, "commit", "--allow-empty", "-qm", "initial")
    git(path, "push", "origin", "HEAD:refs/heads/main")
    monkeypatch.chdir(path)
    return path


def commit(repo: Path, message: str = "next", **files: str) -> str:
    for name, content in files.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    git(repo, "add", ".")
    git(repo, "commit", "--allow-empty", "-qm", message)
    return git(repo, "rev-parse", "HEAD")


def guard(
    monkeypatch: pytest.MonkeyPatch, local: str, remote: str = ZERO, url: str = PUBLIC
) -> int:
    monkeypatch.setattr(
        sys,
        "stdin",
        io.StringIO(f"refs/heads/topic {local} refs/heads/topic {remote}\n"),
    )
    return alpha.main(["pre-push-guard", "origin", url], "tbhbdev alpha")


@pytest.mark.parametrize("kind", ["diverged", "forward", "new", "delete", "missing"])
def test_ancestry(
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    kind: str,
) -> None:
    base = git(repo, "rev-parse", "HEAD")
    head = commit(repo)
    local, remote = {
        "diverged": (base, head),
        "forward": (head, base),
        "new": (head, ZERO),
        "delete": (ZERO, base),
        "missing": (head, "f" * 40),
    }[kind]
    denied = kind in {"diverged", "missing"}
    assert guard(monkeypatch, local, remote) == int(denied)
    out, err = capsys.readouterr()
    assert out == ""
    if denied:
        assert (
            err == "refs/heads/topic: non-fast-forward push refused; "
            "push a new branch instead\n"
        )
    else:
        assert err == ""


@pytest.mark.parametrize("where", ["message", "line", "path", "rename"])
def test_private_hits(
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    where: str,
) -> None:
    if where == "rename":
        commit(repo, **{"old.txt": "ordinary\n"})
        git(repo, "push", "origin", "HEAD:refs/heads/main")
        git(repo, "mv", "old.txt", PRIVATE)
    head = commit(
        repo,
        PRIVATE if where == "message" else "ordinary",
        **(
            {"file.txt": f"safe\n{PRIVATE} secret context\n"}
            if where == "line"
            else {PRIVATE: "ordinary\n"}
            if where == "path"
            else {}
        ),
    )
    assert guard(monkeypatch, head) == 1
    out, err = capsys.readouterr()
    location = {
        "message": "message",
        "line": "'file.txt':2",
        "path": "path",
        "rename": "path",
    }[where]
    assert out == ""
    assert f"{head[:12]} {location}: {PRIVATE!r}" in err
    assert "secret context" not in err


@pytest.mark.parametrize(
    "url",
    [
        f"https://github.com/example/{PRIVATE}.git",
        "git@example.test:org/example-lab.git",
    ],
)
def test_private_target(
    repo: Path, config_file: Path, monkeypatch: pytest.MonkeyPatch, url: str
) -> None:
    config_file.parent.mkdir(parents=True, exist_ok=True)
    config_file.write_text('[pr_check]\nprivate_repos = ["example-lab"]\n')
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_file.parent.parent))
    head = commit(repo, PRIVATE)
    assert guard(monkeypatch, head, url=url) == 0


def test_removed_and_already_remote_lines(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    commit(repo, PRIVATE, **{"file.txt": f"{PRIVATE}\n"})
    git(repo, "push", "origin", "HEAD:refs/heads/main")
    head = commit(repo, **{"file.txt": "safe\n"})
    assert guard(monkeypatch, head) == 0


@pytest.mark.parametrize(
    "url",
    [
        "not a URL",
        "",
        "/",
        f"https://bad host/{PRIVATE}",
        f"https://{PRIVATE}",
        f"://{PRIVATE}",
        f"https://[bad/{PRIVATE}",
    ],
)
def test_unknown_target_is_public(
    repo: Path, monkeypatch: pytest.MonkeyPatch, url: str
) -> None:
    head = commit(repo, PRIVATE)
    assert guard(monkeypatch, head, url=url) == 1


def test_no_bypass(
    repo: Path, monkeypatch: pytest.MonkeyPatch, config_file: Path
) -> None:
    monkeypatch.setenv("TBHBDEV_PRE_PUSH_GUARD_BYPASS", "1")
    config_file.parent.mkdir(parents=True, exist_ok=True)
    config_file.write_text("[pr_check]\npre_push_guard_bypass = true\n")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_file.parent.parent))
    git(repo, "config", "tbhbdev.prePushGuardBypass", "true")
    assert guard(monkeypatch, commit(repo, PRIVATE)) == 1


def test_configured_token(
    repo: Path, monkeypatch: pytest.MonkeyPatch, config_file: Path
) -> None:
    config_file.parent.mkdir(parents=True, exist_ok=True)
    config_file.write_text('[pr_check]\nprivate_repos = ["example-lab"]\n')
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_file.parent.parent))
    assert guard(monkeypatch, commit(repo, "example-lab")) == 1


def test_bad_input_and_git_failure(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    del repo
    monkeypatch.setattr(sys, "stdin", io.StringIO("broken\n"))
    assert alpha.main(["pre-push-guard", "origin", PUBLIC], "p") == 1
    assert capsys.readouterr() == (
        "",
        "pre-push-guard: cannot inspect push; push refused\n",
    )
    assert guard(monkeypatch, "f" * 40) == 1
    assert capsys.readouterr() == (
        "",
        "pre-push-guard: cannot inspect push; push refused\n",
    )


def test_empty_and_duplicate_refs(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    head = commit(repo, PRIVATE)
    row = f"refs/heads/topic {head} refs/heads/topic {ZERO}\n"
    monkeypatch.setattr(sys, "stdin", io.StringIO(row + row))
    assert alpha.main(["pre-push-guard", "origin", PUBLIC], "p") == 1
    assert len(capsys.readouterr().err.splitlines()) == 1
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    assert alpha.main(["pre-push-guard", "origin", PUBLIC], "p") == 0


def test_url_fallback_and_ancestry_on_private_target(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = git(repo, "rev-parse", "HEAD")
    head = commit(repo, PRIVATE)
    url = f"https://github.com/example/{PRIVATE}.git"
    git(repo, "remote", "set-url", "origin", url)
    assert guard(monkeypatch, head, url="") == 0
    assert guard(monkeypatch, base, head, url=url) == 1


def test_patch_locations(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    name = 'a"b.txt'
    commit(repo, **{name: "one\n" + "context\n" * 15 + "end\n"})
    git(repo, "push", "origin", "HEAD:refs/heads/main")
    head = commit(repo, **{name: "one\n" + "context\n" * 15 + PRIVATE})
    assert guard(monkeypatch, head) == 1
    assert f"{name!r}:17: {PRIVATE!r}" in capsys.readouterr().err


def test_safe_git_and_patch_parser(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    original = pg.git
    calls: list[tuple[str, ...]] = []

    def observed(*args: str) -> tuple[int, str]:
        calls.append(args)
        return original(*args)

    monkeypatch.setattr(pg, "git", observed)
    head = commit(repo, **{"file.txt": "text\n"})
    assert guard(monkeypatch, head, git(repo, "rev-parse", "HEAD~")) == 0
    assert calls
    assert all(args[:8] == pg.SAFE for args in calls)
    assert any("--no-ext-diff" in args and "--no-textconv" in args for args in calls)
    # The query shapes keep metadata out of messages and paths, include root
    # commits and merges, and avoid quoting or rename settings from git config.
    queries = {" ".join(args[8:]) for args in calls}
    assert f"show -s --format=%B {head}" in queries
    assert (
        "diff-tree --root -r -m --no-commit-id --name-only --diff-filter=AR -z -M "
        f"{head}"
    ) in queries
    assert (
        "-c core.quotePath=false show --format= -p --no-ext-diff --no-textconv "
        "--no-color --find-renames --diff-merges=first-parent "
        f"--src-prefix=a/ --dst-prefix=b/ {head}"
    ) in queries
    assert pg.added_lines(
        "diff --git a/x b/x\n+++ b/x\n@@ -1 +3,2 @@\n-old\n+++ b/content\n"
        "+second\n\\ No newline at end of file\n"
        "diff --git a/y b/y\n+++ b/y\n@@ -0,0 +1 @@\n+last\n"
    ) == [("x", 3, "++ b/content"), ("x", 4, "second"), ("y", 1, "last")]


def test_patch_without_path() -> None:
    assert pg.added_lines("@@ -0,0 +1 @@\n+text\n") == [("", 1, "text")]


def test_failed_origin_lookup_does_not_trust_output(
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def failed(*_args: str) -> tuple[int, str]:
        return 128, f"https://example.test/{PRIVATE}.git"

    del repo
    monkeypatch.setattr(pg.pn, "git", failed)
    assert pg.pn.private_names() == pg.pn.Names(["example-host", PRIVATE], set())


@pytest.mark.parametrize("bad_local", [True, False])
def test_invalid_object_is_refused(
    repo: Path, monkeypatch: pytest.MonkeyPatch, *, bad_local: bool
) -> None:
    head = commit(repo)
    local, old = ("X" * 40, ZERO) if bad_local else (head, "X" * 40)
    assert guard(monkeypatch, local, old) == 1


def test_delete_does_not_hide_later_ref(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    head = commit(repo, PRIVATE)
    monkeypatch.setattr(
        sys,
        "stdin",
        io.StringIO(
            f"(delete) {ZERO} refs/heads/old {head}\n"
            f"refs/heads/topic {head} refs/heads/topic {ZERO}\n"
        ),
    )
    assert alpha.main(["pre-push-guard", "origin", PUBLIC], "p") == 1


def test_paths_keep_spaces_and_separate_entries(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    head = commit(
        repo,
        **{
            f"{PRIVATE} {PRIVATE}": "safe\n",
            f"nested/{PRIVATE}": "safe\n",
        },
    )
    assert guard(monkeypatch, head) == 1
    lines = capsys.readouterr().err.splitlines()
    assert len(lines) == 2
    assert all(f"{head[:12]} path: {PRIVATE!r}" in line for line in lines)


def test_help_uses_supplied_program(
    plain: None, capsys: pytest.CaptureFixture[str]
) -> None:
    del plain
    assert pg.main(["--help"], "example guard") == 1
    assert capsys.readouterr() == ("", "usage: example guard REMOTE URL\n")


def test_target_url_normalization(
    repo: Path, monkeypatch: pytest.MonkeyPatch, config_file: Path
) -> None:
    head = commit(repo, PRIVATE)
    assert guard(monkeypatch, head, url=f"  https://example.test/{PRIVATE}.git/\n") == 0
    config_file.parent.mkdir(parents=True, exist_ok=True)
    config_file.write_text('[pr_check]\nprivate_repos = ["example-lab"]\n')
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_file.parent.parent))
    assert guard(monkeypatch, head, url="https://example.test/example-labX") == 1


def test_git_filters_never_run(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    marker = repo / "filter-ran"
    script = repo / "filter.sh"
    script.write_text(f'#!/bin/sh\nprintf ran > "{marker}"\n')
    script.chmod(0o755)
    git(repo, "config", "core.fsmonitor", str(script))
    git(repo, "config", "diff.external", str(script))
    git(repo, "config", "diff.example.textconv", str(script))
    head = commit(
        repo, **{".gitattributes": "*.txt diff=example\n", "file.txt": "text\n"}
    )
    assert guard(monkeypatch, head) == 0
    assert not marker.exists()


def test_patch_headers_without_hunk_add_nothing() -> None:
    assert pg.added_lines("+++ b/file.txt\n+outside a hunk\n") == []


def test_spaces_belong_to_one_path(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    head = commit(repo, **{f"{PRIVATE} {PRIVATE}": "safe\n"})
    assert guard(monkeypatch, head) == 1
    assert len(capsys.readouterr().err.splitlines()) == 1


def test_rename_preserves_remote_lines(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    commit(repo, **{"old.txt": f"{PRIVATE}\n"})
    git(repo, "push", "origin", "HEAD:refs/heads/main")
    git(repo, "mv", "old.txt", "new.txt")
    head = commit(repo)
    assert guard(monkeypatch, head) == 0


def test_private_target_checks_every_ref(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = git(repo, "rev-parse", "HEAD")
    head = commit(repo)
    monkeypatch.setattr(
        sys,
        "stdin",
        io.StringIO(
            f"refs/heads/new {head} refs/heads/new {ZERO}\n"
            f"refs/heads/topic {base} refs/heads/topic {head}\n"
        ),
    )
    assert (
        alpha.main(["pre-push-guard", "origin", f"https://example.test/{PRIVATE}"], "p")
        == 1
    )


@pytest.mark.parametrize("remote", ["-h", "--help"])
def test_remote_name_cannot_skip_checks(
    repo: Path, monkeypatch: pytest.MonkeyPatch, remote: str
) -> None:
    head = commit(repo, PRIVATE)
    monkeypatch.setattr(
        sys, "stdin", io.StringIO(f"refs/heads/topic {head} refs/heads/topic {ZERO}\n")
    )
    assert alpha.main(["pre-push-guard", remote, PUBLIC], "p") == 1


@pytest.mark.parametrize(
    "text",
    [
        "example-private",
        PRIVATE,
        f"{PRIVATE}-x",
        "example-host",
    ],
)
def test_placeholder_and_host_names(
    repo: Path, monkeypatch: pytest.MonkeyPatch, text: str
) -> None:
    write_host_names(Path.home(), PRIVATE, f"{PRIVATE}-x")
    assert guard(monkeypatch, commit(repo, **{"file.txt": text})) == int(
        text != "example-private"
    )


@pytest.mark.parametrize(
    "kind", ["missing", "directory", "empty", "comments", "invalid"]
)
def test_host_list_failure_refuses(
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    kind: str,
    config_file: Path,
) -> None:
    config_file.parent.mkdir(parents=True, exist_ok=True)
    config_file.write_text(f'[pr_check]\nprivate_repos = ["{PRIVATE}"]\n')
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_file.parent.parent))
    path = Path.home() / ".local/state/tbhb/private-repos.txt"
    break_host_names(path, kind)
    head = commit(repo)
    assert guard(monkeypatch, head) == 2
    assert capsys.readouterr() == (
        "",
        f"private names: cannot read a nonempty list from {path}\n",
    )
    assert guard(monkeypatch, head, url=f"https://example.test/{PRIVATE}") == 0


@pytest.mark.parametrize("variable", ["SKIP", "PREK_SKIP"])
@pytest.mark.parametrize("value", ["", "sensitive-value", "0"])
def test_skip_refused(
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    variable: str,
    value: str,
) -> None:
    monkeypatch.setenv(variable, value)
    assert guard(monkeypatch, commit(repo)) == int(bool(value))
    assert capsys.readouterr() == (
        "",
        f"pre-push-guard: {variable} is set; push refused\n" if value else "",
    )


def test_host_path_words(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = Path.home() / ".local/state/tbhb/private-repos.txt"
    path.write_text(
        "# Bare-word exempt, flagged only in path position: example\n"
        "example\nexample-host\n"
    )
    assert guard(monkeypatch, commit(repo, "example")) == 0
    assert guard(monkeypatch, commit(repo, "org/EXAMPLE")) == 1
    assert guard(monkeypatch, commit(repo, "example.git")) == 1


def test_push_hit_never_reaches_remote(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hooks = repo / ".git/hooks"
    (hooks / "pre-push").write_text("#!/bin/sh\n# File generated by prek\nexit 0\n")
    (hooks / "pre-push").chmod(0o755)
    install.install_pre_push_hook(hooks)
    binary = repo / "bin"
    binary.mkdir()
    tool = binary / "tbhbdev"
    tool.write_text(
        f"#!{sys.executable}\n"
        "import sys\nfrom tbhbdev.cli import main\nsys.exit(main())\n"
    )
    tool.chmod(0o755)
    monkeypatch.setenv("PATH", f"{binary}:{os.environ['PATH']}")
    monkeypatch.setenv("PYTHONPATH", pythonpath(Path(pg.__file__).parents[2]))
    child_env = dict(os.environ)
    child_env.pop("MUTANT_UNDER_TEST", None)
    subprocess.run(
        ["git", "push", "origin", "HEAD:refs/heads/allowed"],
        cwd=repo,
        env=child_env,
        capture_output=True,
        check=True,
    )
    commit(repo, **{"file.txt": "example-host\n"})
    done = subprocess.run(
        ["git", "push", "origin", "HEAD:refs/heads/blocked"],
        env=child_env,
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode != 0
    assert "example-host" in done.stderr, done.stderr
    assert "Traceback" not in done.stderr
    remote = repo.parent / "remote.git"
    assert git(remote, "for-each-ref", "refs/heads/blocked") == ""


@pytest.mark.parametrize(("prefix", "suffix"), [("x", ""), ("", "-x")])
def test_guard_configured_name_boundaries(
    repo: Path, monkeypatch: pytest.MonkeyPatch, prefix: str, suffix: str
) -> None:
    text = f"{prefix}{PRIVATE}{suffix}"
    assert guard(monkeypatch, commit(repo, text)) == 0
    write_host_names(Path.home(), text)
    assert guard(monkeypatch, commit(repo, text)) == 1


def test_repository_name_without_a_final_component() -> None:
    assert pg.pn.repository_name("org/:") is None


@pytest.mark.parametrize("public_push", [True, False])
def test_fallback_uses_push_url(
    repo: Path, monkeypatch: pytest.MonkeyPatch, public_push: bool
) -> None:
    private = f"https://example.test/{PRIVATE}"
    git(repo, "remote", "set-url", "origin", private if public_push else PUBLIC)
    git(
        repo,
        "remote",
        "set-url",
        "--push",
        "origin",
        PUBLIC if public_push else private,
    )
    assert guard(monkeypatch, commit(repo, PRIVATE), url="") == int(public_push)


@pytest.mark.parametrize("ref", ["refs/heads/main", "refs/heads/master"])
@pytest.mark.parametrize("kind", ["forward", "new", "delete"])
def test_protected_branch_refused(
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    ref: str,
    kind: str,
) -> None:
    base = git(repo, "rev-parse", "HEAD")
    head = commit(repo)
    local, remote = {
        "forward": (head, base),
        "new": (head, ZERO),
        "delete": (ZERO, base),
    }[kind]
    monkeypatch.setattr(sys, "stdin", io.StringIO(f"HEAD {local} {ref} {remote}\n"))
    assert alpha.main(["pre-push-guard", "origin", PUBLIC], "tbhbdev alpha") == 1
    out, err = capsys.readouterr()
    assert out == ""
    assert err == (
        f"{ref}: push to a protected branch refused; "
        "push a new branch and open a pull request\n"
    )


def test_protected_branch_refused_beside_other_refs(
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    base = git(repo, "rev-parse", "HEAD")
    head = commit(repo)
    monkeypatch.setattr(
        sys,
        "stdin",
        io.StringIO(
            f"refs/heads/topic {head} refs/heads/topic {ZERO}\n"
            f"refs/heads/topic {head} refs/heads/main {base}\n"
        ),
    )
    assert alpha.main(["pre-push-guard", "origin", PUBLIC], "tbhbdev alpha") == 1
    assert capsys.readouterr().err.startswith("refs/heads/main: push to a protected")


def test_protected_branch_does_not_hide_later_refs(
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    base = git(repo, "rev-parse", "HEAD")
    head = commit(repo)
    monkeypatch.setattr(
        sys,
        "stdin",
        io.StringIO(
            f"refs/heads/topic {head} refs/heads/main {base}\n"
            f"refs/heads/topic {base} refs/heads/topic {head}\n"
        ),
    )
    assert alpha.main(["pre-push-guard", "origin", PUBLIC], "tbhbdev alpha") == 1
    err = capsys.readouterr().err
    assert "refs/heads/main: push to a protected" in err
    assert "refs/heads/topic: non-fast-forward push refused" in err


@pytest.mark.parametrize(
    "ref",
    [
        "refs/heads/maintenance",
        "refs/heads/main2",
        "refs/tags/main",
        "refs/heads/x/main",
    ],
)
def test_similar_names_are_not_protected(
    repo: Path, monkeypatch: pytest.MonkeyPatch, ref: str
) -> None:
    head = commit(repo)
    monkeypatch.setattr(sys, "stdin", io.StringIO(f"HEAD {head} {ref} {ZERO}\n"))
    assert alpha.main(["pre-push-guard", "origin", PUBLIC], "tbhbdev alpha") == 0

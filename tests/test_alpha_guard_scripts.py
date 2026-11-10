# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns
"""Tests for tbhbdev alpha guard-scripts, on a real filesystem under tmp_path.

HOME is always a directory in tmp_path, never the real home.
"""
# ruff: noqa: S108 -- the paths are test data under /tmp that no test creates

import io
import json
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from tbhbdev import alpha
from tbhbdev.alpha import guard_scripts as guard

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

type Json = dict[str, object]

PLACE = (
    "Write scripts for the operator to ~/.local/libexec/tbhb/"
    "YYYY-MM-DD-<owner>-<slug>.sh. A revision is a new file with a .vN suffix, "
    "because a handed-over script is never edited. "
    "Run logs go in ~/.local/state/tbhb/<same name>/."
)
SHEBANG = "#!/bin/sh\n"
NEW = "2026-11-09-coord-foo.sh"


@pytest.fixture(autouse=True)
def isolated_work_tree_roots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Git metadata outside the scratch filesystem isn't part of these cases."""
    original = guard.work_tree_names

    def names(path: str) -> list[str] | None:
        result = original(path)
        parents = (p for p in Path(path).parents if p.is_relative_to(tmp_path))
        if any((parent / ".git").exists() for parent in parents):
            return result
        return None

    monkeypatch.setattr(guard, "work_tree_names", names)


@pytest.fixture
def home(tmp_path: Path) -> Path:
    path = tmp_path / "home"
    (path / "work").mkdir(parents=True)
    return path


@pytest.fixture
def libexec(home: Path) -> Path:
    path = home / ".local" / "libexec" / "tbhb"
    path.mkdir(parents=True)
    return path


def env_of(home: Path, tmpdir: str | None = None) -> dict[str, str]:
    return {"HOME": str(home)} | ({} if tmpdir is None else {"TMPDIR": tmpdir})


def bash(home: Path, command: str) -> Json:
    return {
        "tool_name": "Bash",
        "tool_input": {"command": command},
        "cwd": str(home / "work"),
    }


def tool(home: Path, name: str, **fields: object) -> Json:
    return {"tool_name": name, "tool_input": fields, "cwd": str(home / "work")}


def outside(path: object) -> str:
    return f"Scripts for the operator don't go in {path}. {PLACE}"


def places(home: Path, tmpdir: str = "") -> guard.Place:
    return guard.Place(str(home / "work"), guard.normalize(str(home)), tmpdir)


# The refusal messages, spelled out.


def test_the_messages_are_exact(home: Path, libexec: Path) -> None:
    old = libexec / NEW
    old.write_text(SHEBANG)
    env = env_of(home)
    assert guard.first_refusal(tool(home, "Write", file_path=str(old)), env) == (
        f"{old} already exists, and a handed-over script is never edited. "
        "Write the revision to a new file with a .vN suffix, such as "
        "YYYY-MM-DD-<owner>-<slug>.v2.sh."
    )
    bad = libexec / "run.sh"
    assert guard.first_refusal(tool(home, "Write", file_path=str(bad)), env) == (
        f"{bad} isn't a valid name for a script. Name it "
        "~/.local/libexec/tbhb/YYYY-MM-DD-<owner>-<slug>.sh, with a real "
        "date and no subdirectory. A revision is a new file with a .vN suffix."
    )
    drafts = home / "drafts-x"
    (drafts / ".git").mkdir(parents=True)
    path = drafts / "a.sh"
    assert guard.first_refusal(tool(home, "Write", file_path=str(path)), env) == (
        f"A drafts repository doesn't hold scripts: {path}. "
        "Write scripts for the operator to "
        "~/.local/libexec/tbhb/YYYY-MM-DD-<owner>-<slug>.sh. A revision is a new "
        "file with a .vN suffix, because a handed-over script is never edited. "
        "Run logs go in ~/.local/state/tbhb/<same name>/."
    )
    cache = home / ".cache" / "a.sh"
    assert guard.first_refusal(tool(home, "Write", file_path=str(cache)), env) == (
        f"Scripts for the operator don't go in {cache}. "
        "Write scripts for the operator to "
        "~/.local/libexec/tbhb/YYYY-MM-DD-<owner>-<slug>.sh. A revision is a new "
        "file with a .vN suffix, because a handed-over script is never edited. "
        "Run logs go in ~/.local/state/tbhb/<same name>/."
    )


# Rule 6: a script under $HOME or /tmp, outside everything allowed.


@pytest.mark.parametrize(
    ("command", "written"),
    [
        ("echo x > ~/.cache/coord/x/run.sh", ".cache/coord/x/run.sh"),
        ("echo x >> ~/.cache/a.sh", ".cache/a.sh"),
        ("echo x >~/.cache/a.sh", ".cache/a.sh"),
        ("echo x >| ~/.cache/a.sh", ".cache/a.sh"),
        ("echo x >|~/.cache/a.sh", ".cache/a.sh"),
        ("echo x &> ~/.cache/a.sh", ".cache/a.sh"),
        ("echo x &>~/.cache/a.sh", ".cache/a.sh"),
        ("echo x &>> ~/.cache/a.sh", ".cache/a.sh"),
        ("echo x 2> ~/.cache/a.sh", ".cache/a.sh"),
        ("echo x 12>~/.cache/a.sh", ".cache/a.sh"),
        ("echo x 2>&1 > ~/.cache/a.sh", ".cache/a.sh"),
        ("echo x >&~/.cache/a.sh", ".cache/a.sh"),
        ("> ~/.cache/a.sh", ".cache/a.sh"),
        ("A=1 cat > ~/.cache/a.sh", ".cache/a.sh"),
        ("> ~/.cache/a.sh cat", ".cache/a.sh"),
        ("echo x | tee ~/.cache/a.sh", ".cache/a.sh"),
        ("echo x | tee -a ~/.cache/a.sh", ".cache/a.sh"),
        ("echo x | tee /dev/null ~/.cache/a.sh", ".cache/a.sh"),
        ("echo x | sudo tee ~/.cache/a.sh", ".cache/a.sh"),
        ("cp ./t.sh ~/.cache/", ".cache/t.sh"),
        ("cp t.sh ~/.cache/renamed", ".cache/renamed"),
        ("cp -a t.sh ~/.cache/x", ".cache/x"),
        ("cp -t ~/.cache t.sh", ".cache/t.sh"),
        ("cp -t~/.cache t.sh", ".cache/t.sh"),
        ("cp --target-directory=~/.cache t.sh", ".cache/t.sh"),
        ("cp --target-directory ~/.cache t.sh", ".cache/t.sh"),
        ("cp -t ~/.cache a.sh b.sh", ".cache/a.sh"),
        ("cp a.sh b.sh ~/.cache", ".cache/a.sh"),
        ("cp a.sh b.sh ~/.cache", ".cache/b.sh"),
        ("mv t.sh ~/.cache/", ".cache/t.sh"),
        ("mv t.sh ~/Desktop/x.sh", "Desktop/x.sh"),
        ("install -m755 t.sh ~/bin/x.sh", "bin/x.sh"),
        ("install -m 755 t.sh ~/bin/x", "bin/x"),
        ("install -o me -g us t.sh ~/bin/x", "bin/x"),
        ("install -D -m755 t.sh ~/bin/", "bin/t.sh"),
        ("ln -s ~/x.sh ~/.cache/link", ".cache/link"),
        ("ln t.sh ~/.cache/link", ".cache/link"),
        ("ln -sf t.sh ~/.cache/", ".cache/t.sh"),
        ("ln -t ~/.cache t.sh", ".cache/t.sh"),
        ("bash -c 'echo x > ~/.cache/a.sh'", ".cache/a.sh"),
        ('sh -lc "cp t.sh ~/.cache/"', ".cache/t.sh"),
        ("eval 'echo x > ~/.cache/a.sh'", ".cache/a.sh"),
        ("sudo -E tee ~/.cache/a.sh", ".cache/a.sh"),
        ("cd /x && echo x > ~/.cache/a.sh", ".cache/a.sh"),
        ("echo $(echo x > ~/.cache/a.sh)", ".cache/a.sh"),
        ("echo x > $HOME/.cache/a.sh", ".cache/a.sh"),
        ("echo x > ${HOME}/.cache/a.sh", ".cache/a.sh"),
        ("echo x > ~/.cache/../.cache/a.sh", ".cache/a.sh"),
        ("echo x > ../.cache/a.sh", ".cache/a.sh"),
        ("echo x > ../.cache/a.sh && true", ".cache/a.sh"),
        ("echo x > ~/.local/state/tbhb/a.sh", ".local/state/tbhb/a.sh"),
        ("echo x > ~/.local/libexec/other/a.sh", ".local/libexec/other/a.sh"),
        ("echo x > ~", ""),
        ("cp ~/.cache/a.sh .", "work/a.sh"),
    ],
)
def test_a_script_under_home_is_refused(home: Path, command: str, written: str) -> None:
    (home / "work" / "t.sh").write_text("")
    if written in {"", ".cache/a.sh"} and "~/.cache/a.sh" not in command:
        pass
    # `echo x > ~` writes the home directory itself, which isn't a script.
    expected = None if written == "" else outside(f"{home}/{written}")
    found = guard.first_refusal(bash(home, command), env_of(home))
    if "cp a.sh b.sh" in command:
        assert found in {outside(f"{home}/.cache/a.sh"), outside(f"{home}/.cache/b.sh")}
    else:
        assert found == expected


def test_both_copies_are_judged(home: Path) -> None:
    place = places(home)
    found = list(guard.bash_writes("cp a.sh b.sh ~/.cache", place))
    assert found == [
        guard.Write(f"{home}/.cache/a.sh", script=True),
        guard.Write(f"{home}/.cache/b.sh", script=True),
    ]


def test_a_heredoc_that_starts_with_a_shebang_makes_a_script(home: Path) -> None:
    command = "cat > ~/.cache/x <<'E'\n#!/bin/sh\necho hi\nE\n"
    found = guard.first_refusal(bash(home, command), env_of(home))
    assert found == outside(f"{home}/.cache/x")
    tee = "cat <<E | tee ~/.cache/x\n#!/bin/sh\nE\n"
    assert guard.first_refusal(bash(home, tee), env_of(home)) is None
    fed = "tee ~/.cache/x <<E\n#!/bin/sh\nE\n"
    assert guard.first_refusal(bash(home, fed), env_of(home)) == outside(
        f"{home}/.cache/x"
    )
    first = "cat > ~/.cache/x <<'E'\necho hi\n#!/bin/sh\nE\n"
    assert guard.first_refusal(bash(home, first), env_of(home)) is None
    dash = "cat > ~/.cache/x <<-E\n#!/bin/sh\nE\n"
    assert guard.first_refusal(bash(home, dash), env_of(home)) == outside(
        f"{home}/.cache/x"
    )


def test_a_heredoc_fed_to_a_shell_is_scanned(home: Path) -> None:
    command = "bash <<'E'\necho x > ~/.cache/a.sh\nE\n"
    assert guard.first_refusal(bash(home, command), env_of(home)) == outside(
        f"{home}/.cache/a.sh"
    )


def test_ssh_runs_elsewhere_so_it_is_not_judged(home: Path) -> None:
    command = "ssh host 'cat > ~/.cache/a.sh'"
    assert guard.first_refusal(bash(home, command), env_of(home)) is None
    assert (
        guard.first_refusal(bash(home, "ssh host <<E\n> ~/a.sh\nE\n"), env_of(home))
        is None
    )


def test_a_shebang_file_without_a_sh_name_is_a_script(home: Path) -> None:
    state = home / ".local" / "state" / "foo"
    state.mkdir(parents=True)
    path = state / "run"
    found = guard.first_refusal(
        tool(home, "Write", file_path=str(path), content=SHEBANG), env_of(home)
    )
    assert found == outside(path)
    plain = tool(home, "Write", file_path=str(path), content="#\n")
    assert guard.first_refusal(plain, env_of(home)) is None
    assert (
        guard.first_refusal(tool(home, "Write", file_path=str(path)), env_of(home))
        is None
    )


@pytest.mark.parametrize("key", ["content", "CodeContent", "Content"])
def test_write_content_fields(home: Path, key: str) -> None:
    data = tool(home, "write_file", Path=str(home / "x"), **{key: SHEBANG})
    assert guard.first_refusal(data, env_of(home)) == outside(home / "x")


def test_a_copied_source_is_read_for_its_shebang(home: Path) -> None:
    work = home / "work"
    (work / "t").write_text(SHEBANG + "echo\n")
    (work / "plain").write_text("#\n")
    (work / "one").write_text("#")
    (work / "empty").write_text("")
    (work / "dir").mkdir()
    env = env_of(home)
    found = guard.first_refusal(bash(home, "cp t ~/.cache/x"), env)
    assert found == outside(f"{home}/.cache/x")
    for source in ["plain", "one", "empty", "dir", "missing"]:
        assert guard.first_refusal(bash(home, f"cp {source} ~/.cache/x"), env) is None
    found = guard.first_refusal(bash(home, "mv ~/work/t ~/.cache/x"), env)
    assert found == outside(f"{home}/.cache/x")


def test_a_fifo_source_is_not_opened(home: Path) -> None:
    fifo = home / "work" / "pipe"
    os.mkfifo(fifo)
    command = "cp pipe ~/.cache/x"
    assert guard.first_refusal(bash(home, command), env_of(home)) is None
    assert not guard.has_shebang(str(fifo))


def test_an_unreadable_source_is_not_a_script(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (home / "work" / "dir").mkdir()

    def is_file(_self: Path) -> bool:
        return True

    monkeypatch.setattr(Path, "is_file", is_file)
    assert not guard.has_shebang(str(home / "work" / "dir"))


def test_has_shebang_reads_two_bytes(home: Path) -> None:
    path = home / "f"
    path.write_text("#!/bin/sh\n")
    assert guard.has_shebang(str(path))
    path.write_text("#/")
    assert not guard.has_shebang(str(path))
    path.write_text("!#")
    assert not guard.has_shebang(str(path))


@pytest.mark.parametrize(
    "command",
    [
        "echo x > ~/.cache/x.json",
        "echo x > ~/.cache/x",
        "cp t.json ~/.cache/",
        "cp t ~/.cache/",
        "cp",
        "cp a",
        "cp -r",
        "ln -s",
        "install -d ~/.cache/x.sh",
        "install -d ~/.cache/x.sh ~/.cache/y.sh",
        "install --directory ~/.cache/x.sh",
        "echo x 2>&1",
        "echo x >&2",
        "echo x 2>&-",
        "echo x >&-",
        "echo x > /dev/null",
        "echo x 2> /dev/null",
        "echo x >",
        "echo x > ",
        "cat ~/.cache/a.sh",
        "ls ~/.cache/a.sh",
        "chmod +x ~/.cache/a.sh",
        "bash ~/.cache/a.sh",
        "echo x | tee",
        "echo x | tee -a",
        "echo '> ~/.cache/a.sh'",
        "echo '#!/bin/sh' ~/.cache/x",
        "echo x > /usr/local/bin/x.sh",
        "echo x > /var/folders/x/T/a.sh",
        "echo x > /etc/a.sh",
        "echo x > /tmp2/a.sh",
        "echo x > /private/tmp2/a.sh",
        "echo x > /private/a.sh",
        "echo x # > ~/.cache/a.sh",
        "bash -c",
        "bash -c ''",
        "bash script.sh",
        "eval",
        "",
        "   ",
        "true",
        "A=1",
        "sudo",
    ],
)
def test_these_commands_are_allowed(home: Path, command: str) -> None:
    assert guard.first_refusal(bash(home, command), env_of(home)) is None


def test_a_numbered_target_is_a_file(home: Path) -> None:
    found = guard.first_refusal(bash(home, "echo x > 2.sh"), env_of(home))
    assert found == outside(home / "work" / "2.sh")
    word = guard.RedirectScanner("echo a2>f").run()
    assert word == [["echo", "a2", ">", "f"]]
    assert guard.redirects(word[0]) == (["f"], ["echo", "a2"])


def test_redirect_scanner() -> None:
    cases = {
        "cat >f": [["cat", ">", "f"]],
        "cat > f": [["cat", ">", "f"]],
        "cat >> f": [["cat", ">>", "f"]],
        "cat >| f": [["cat", ">|", "f"]],
        "cat &> f": [["cat", "&>", "f"]],
        "cat &>> f": [["cat", "&>>", "f"]],
        "cat 2>&1": [["cat", ">&", "1"]],
        "cat 2> f": [["cat", ">", "f"]],
        "cat 12>f": [["cat", ">", "f"]],
        "a && b": [["a"], ["b"]],
        "a & b": [["a"], ["b"]],
        "a | b": [["a"], ["b"]],
        "a ; b > f": [["a"], ["b", ">", "f"]],
        "cat 'a>b'": [["cat", "a>b"]],
        "cat <f": [["cat", "<f"]],
    }
    for text, expected in cases.items():
        assert guard.RedirectScanner(text).run() == expected, text
    assert all(
        isinstance(word, guard.Redirect)
        for word in guard.RedirectScanner("cat >f").run()[0][1:2]
    )
    assert not isinstance(guard.RedirectScanner("cat >f").run()[0][2], guard.Redirect)
    assert not isinstance(guard.RedirectScanner("cat >f").run()[0][0], guard.Redirect)


def test_redirects_split_targets_from_words() -> None:
    redirect = guard.Redirect
    assert guard.redirects(["a", redirect(">"), "f", "b"]) == (["f"], ["a", "b"])
    assert guard.redirects([redirect(">&"), "2", "a"]) == ([], ["a"])
    assert guard.redirects([redirect(">&"), "-", "a"]) == ([], ["a"])
    assert guard.redirects([redirect(">&"), "2-"]) == ([], [])
    assert guard.redirects([redirect(">&"), "f"]) == (["f"], [])
    assert guard.redirects([redirect(">"), "2"]) == (["2"], [])
    assert guard.redirects([redirect(">")]) == ([], [])
    assert guard.redirects([redirect(">&"), "2x"]) == (["2x"], [])


# Rules 3 to 5.


def test_a_code_repository_work_tree_is_allowed(home: Path) -> None:
    repo = home / "code" / "tools"
    (repo / ".git").mkdir(parents=True)
    path = repo / "bin" / "run.sh"
    data = tool(home, "Write", file_path=str(path))
    assert guard.first_refusal(data, env_of(home)) is None
    assert guard.first_refusal(bash(home, f"cp t.sh {path}"), env_of(home)) is None


def test_a_linked_work_tree_of_a_code_repository_is_allowed(home: Path) -> None:
    main = home / "code" / "tools"
    link = main / ".worktrees" / "wt"
    git_dir = main / ".git" / "worktrees" / "wt"
    git_dir.mkdir(parents=True)
    (git_dir / "commondir").write_text("../..\n")
    link.mkdir(parents=True)
    (link / ".git").write_text(f"gitdir: {git_dir}\n")
    data = tool(home, "Write", file_path=str(link / "run.sh"))
    assert guard.first_refusal(data, env_of(home)) is None


@pytest.mark.parametrize("prefix", ["/tmp", "/private/tmp"])
def test_this_sessions_scratchpad_is_allowed(home: Path, prefix: str) -> None:
    path = f"{prefix}/claude-501/-Users-x-proj/0a1b-2c/scratchpad/run.sh"
    data = tool(home, "Write", file_path=path)
    assert guard.first_refusal(data, env_of(home)) is None
    deeper = f"{prefix}/claude-501/-Users-x-proj/0a1b-2c/scratchpad/a/b.sh"
    assert (
        guard.first_refusal(tool(home, "Write", file_path=deeper), env_of(home)) is None
    )


@pytest.mark.parametrize(
    "path",
    [
        "/tmp/claude-x/p/s/scratchpad/run.sh",
        "/tmp/claude-/p/s/scratchpad/run.sh",
        "/tmp/claude-501/p/scratchpad/run.sh",
        "/tmp/claude-501/p/s/t/scratchpad/run.sh",
        "/tmp/claude-501/p/s/other/run.sh",
        "/tmp/claude-501/p/s/scratchpad.sh",
        "/tmp/claude-501//s/scratchpad/run.sh",
        "/tmp/x/claude-501/p/s/scratchpad/run.sh",
        "/tmp/claude-501/p/s/scratchpad/../run.sh",
        "/tmp/claude-501/../a.sh",
    ],
)
def test_other_paths_in_tmp_are_refused(home: Path, path: str) -> None:
    data = tool(home, "Write", file_path=path)
    expected = guard.normalize(path)
    found = guard.first_refusal(data, env_of(home))
    assert found == outside(expected)


def test_a_run_scoped_tmpdir_is_allowed(home: Path) -> None:
    data = tool(home, "Write", file_path="/tmp/run-1/x/a.sh")
    assert guard.first_refusal(data, env_of(home, "/tmp/run-1")) is None
    assert guard.first_refusal(data, env_of(home, "/private/tmp/run-1/")) is None
    assert guard.first_refusal(data, env_of(home, "/tmp/run-1/x")) is None
    data = tool(home, "Write", file_path="/private/tmp/run-1/a.sh")
    assert guard.first_refusal(data, env_of(home, "/tmp/run-1")) is None


@pytest.mark.parametrize(
    "tmpdir",
    [
        None,
        "",
        "/tmp",
        "/tmp/",
        "/private/tmp",
        "/tmp/run-2",
        "/tmp/run-1/x/y",
        "/tmp/run-10",
        "run-1",
        "/var/folders/T",
    ],
)
def test_a_tmpdir_that_is_not_run_scoped_does_not_allow(
    home: Path, tmpdir: str | None
) -> None:
    data = tool(home, "Write", file_path="/tmp/run-1/a.sh")
    found = guard.first_refusal(data, env_of(home, tmpdir))
    assert found == outside("/tmp/run-1/a.sh")


def test_a_tmpdir_with_parent_references_is_normalized(home: Path) -> None:
    data = tool(home, "Write", file_path="/tmp/a.sh")
    assert guard.first_refusal(data, env_of(home, "/tmp/x/..")) == outside("/tmp/a.sh")
    assert guard.first_refusal(data, env_of(home, "/tmp/../tmp/x/..")) == outside(
        "/tmp/a.sh"
    )


def test_a_tmpdir_outside_tmp_allows_nothing_there(home: Path, tmp_path: Path) -> None:
    run = tmp_path / "run"
    data = tool(home, "Write", file_path=str(run / "a.sh"))
    assert guard.first_refusal(data, env_of(home, str(run))) is None


# Rule 2: drafts repositories.


def test_a_drafts_work_tree_is_refused(home: Path) -> None:
    (home / "drafts-private" / ".git").mkdir(parents=True)
    path = home / "drafts-private" / "drafts" / "a.sh"
    data = tool(home, "Write", file_path=str(path))
    found = guard.first_refusal(data, env_of(home))
    assert found == f"A drafts repository doesn't hold scripts: {path}. {PLACE}"
    other = tool(home, "Write", file_path=str(path.with_suffix(".md")))
    assert guard.first_refusal(other, env_of(home)) is None


def test_the_name_must_start_with_drafts(home: Path) -> None:
    for name in ["my-drafts", "draft", "a-drafts"]:
        (home / name / ".git").mkdir(parents=True)
        data = tool(home, "Write", file_path=str(home / name / "a.sh"))
        assert guard.first_refusal(data, env_of(home)) is None, name
    (home / "drafts").mkdir()
    (home / "drafts" / ".git").mkdir()
    data = tool(home, "Write", file_path=str(home / "drafts" / "a.sh"))
    assert guard.first_refusal(data, env_of(home)) is not None


def make_linked(home: Path, main: str, *, relative: bool, bare: bool = False) -> Path:
    base = home / main
    git_dir = base / ("" if bare else ".git") / "worktrees" / "wt"
    git_dir.mkdir(parents=True)
    (git_dir / "commondir").write_text("../..\n")
    link = home / "elsewhere" / "wt"
    link.mkdir(parents=True)
    target = os.path.relpath(git_dir, link) if relative else str(git_dir)
    (link / ".git").write_text(f"gitdir: {target}\n")
    return link


@pytest.mark.parametrize("relative", [False, True])
def test_a_linked_work_tree_of_a_drafts_repository_is_refused(
    home: Path, *, relative: bool
) -> None:
    link = make_linked(home, "drafts-private", relative=relative)
    data = tool(home, "Write", file_path=str(link / "sub" / "a.sh"))
    found = guard.first_refusal(data, env_of(home))
    assert (
        found == f"A drafts repository doesn't hold scripts: {link}/sub/a.sh. {PLACE}"
    )


def test_a_bare_drafts_repository_names_its_linked_work_trees(home: Path) -> None:
    link = make_linked(home, "drafts.git", relative=False, bare=True)
    data = tool(home, "Write", file_path=str(link / "a.sh"))
    assert guard.first_refusal(data, env_of(home)) is not None


def test_a_linked_work_tree_of_another_repository_is_allowed(home: Path) -> None:
    link = make_linked(home, "tools", relative=False)
    data = tool(home, "Write", file_path=str(link / "a.sh"))
    assert guard.first_refusal(data, env_of(home)) is None


def test_a_drafts_named_linked_work_tree_is_refused_by_its_own_name(home: Path) -> None:
    link = make_linked(home, "tools", relative=False)
    drafty = link.with_name("drafts-wt")
    link.rename(drafty)
    data = tool(home, "Write", file_path=str(drafty / "a.sh"))
    assert guard.first_refusal(data, env_of(home)) is not None


def test_main_repo_names(home: Path) -> None:
    top = home / "wt"
    top.mkdir()
    dot_git = top / ".git"
    dot_git.write_text("gitdir: nowhere\n")
    assert guard.main_repo_names(top, dot_git) == []
    git_dir = home / "main" / ".git" / "worktrees" / "wt"
    git_dir.mkdir(parents=True)
    dot_git.write_text(f"gitdir: {git_dir}\n")
    assert guard.main_repo_names(top, dot_git) == []
    (git_dir / "commondir").write_text("../..\n")
    assert guard.main_repo_names(top, dot_git) == ["main"]
    (git_dir / "commondir").write_text(f"{home}/other.git")
    assert guard.main_repo_names(top, dot_git) == ["other.git"]
    dot_git.write_bytes(b"\xff")
    assert guard.main_repo_names(top, dot_git) == []


@pytest.fixture
def case_sensitive(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make Path lookups match names exactly, as on a case-sensitive filesystem."""

    def exact(method: Callable[[Path], object], missing: object) -> object:
        def lookup(self: Path) -> object:
            if self.name not in {entry.name for entry in self.parent.iterdir()}:
                if isinstance(missing, Exception):
                    raise missing
                return missing
            return method(self)

        return lookup

    for name, missing in (
        ("is_dir", False),
        ("is_file", False),
        ("read_text", FileNotFoundError()),
    ):
        monkeypatch.setattr(Path, name, exact(getattr(Path, name), missing))


@pytest.mark.usefixtures("case_sensitive")
def test_names_match_the_git_entries_exactly(home: Path) -> None:
    upper = home / "upper"
    (upper / ".GIT").mkdir(parents=True)
    assert guard.work_tree_names(str(upper / "a.sh")) is None
    top = home / "wt"
    top.mkdir()
    dot_git = top / ".git"
    git_dir = home / "main" / ".git" / "worktrees" / "wt"
    git_dir.mkdir(parents=True)
    dot_git.write_text(f"gitdir: {git_dir}\n")
    (git_dir / "COMMONDIR").write_text("../..\n")
    assert guard.main_repo_names(top, dot_git) == []


def test_work_tree_names(home: Path) -> None:
    repo = home / "r"
    (repo / ".git").mkdir(parents=True)
    assert guard.work_tree_names(str(repo / "a" / "b" / "c.sh")) == ["r"]
    assert guard.work_tree_names(str(repo / "c.sh")) == ["r"]
    assert guard.work_tree_names(str(home / "nothing" / "c.sh")) is None
    nested = repo / "sub"
    nested.mkdir()
    (nested / ".git").write_text("gitdir: nowhere")
    assert guard.work_tree_names(str(nested / "c.sh")) == ["sub"]
    assert guard.work_tree_names("/c.sh") is None


# Rule 1: the libexec directory.


@pytest.mark.parametrize(
    "name",
    [
        NEW,
        "2026-11-09-coord-foo.v2.sh",
        "2026-11-09-coord-foo.v10.sh",
        "2024-02-29-a-b.sh",
        "2026-11-09-co-ord-foo-bar.sh",
        "2026-1-09-coord-foo.sh",
    ][:-1],
)
def test_a_valid_new_name_is_allowed(home: Path, libexec: Path, name: str) -> None:
    env = env_of(home)
    path = libexec / name
    assert guard.first_refusal(tool(home, "Write", file_path=str(path)), env) is None
    assert guard.first_refusal(bash(home, f"echo x > {path}"), env) is None
    assert guard.first_refusal(bash(home, f"cp t.sh {path}"), env) is None


@pytest.mark.parametrize(
    "name",
    [
        "run.sh",
        "coord-foo.sh",
        "2026-11-09-coord.sh",
        "2026-11-09-coord-.sh",
        "2026-11-09--foo.sh",
        "2026-02-30-coord-foo.sh",
        "2025-02-29-coord-foo.sh",
        "2026-13-01-coord-foo.sh",
        "2026-00-01-coord-foo.sh",
        "2026-01-00-coord-foo.sh",
        "0000-01-01-coord-foo.sh",
        "2026-11-09-Coord-foo.sh",
        "2026-11-09-coord-Foo.sh",
        "2026-11-09-coord_foo.sh",
        "2026-11-09-coord-foo.sh.bak",
        "2026-11-09-coord-foo.bak",
        "2026-11-09-coord-foo",
        "2026-11-09-coord-foo.v.sh",
        "2026-11-09-coord-foo.vx.sh",
        "2026-11-09-coord-foo.v2",
        "2026-11-09-coord-foo.v2.sh.bak",
        "2026-11-09-coord-foo.sh~",
        " 2026-11-09-coord-foo.sh",
        "x2026-11-09-coord-foo.sh",
        "2026-11-09-coord-foo.shx",
        "2026-11-09-coord-foo.v2.v3.sh",
        "02026-11-09-coord-foo.sh",
        "2026-011-09-coord-foo.sh",
        "2026-11-009-coord-foo.sh",
        "20261109-coord-foo.sh",
        "\u0662\u0660\u0662\u0666-\u0661\u0661-\u0660\u0669-coord-foo.sh",
        "2026-11-09-caf\u00e9-foo.sh",
        ".2026-11-09-coord-foo.sh",
    ],
)
def test_a_bad_name_is_refused(home: Path, libexec: Path, name: str) -> None:
    env = env_of(home)
    path = libexec / name
    expected = guard.first_refusal(tool(home, "Write", file_path=str(path)), env)
    assert expected is not None
    assert expected.startswith(f"{path} isn't a valid name for a script.")
    assert (
        guard.first_refusal(bash(home, f"echo '#!/bin/sh' > '{path}'"), env) == expected
    )


def test_a_subdirectory_is_refused(home: Path, libexec: Path) -> None:
    env = env_of(home)
    for path in [libexec / "sub" / NEW, libexec / "a" / "b" / NEW, libexec / NEW / NEW]:
        found = guard.first_refusal(tool(home, "Write", file_path=str(path)), env)
        assert found is not None
        assert found.startswith(f"{path} isn't a valid name")
    (libexec / "sub").mkdir()
    found = guard.first_refusal(bash(home, f"cp t.sh {libexec}/sub/"), env)
    assert found is not None
    assert found.startswith(f"{libexec}/sub/t.sh isn't a valid name")


def test_the_directory_itself_is_not_a_script_name(home: Path, libexec: Path) -> None:
    found = guard.first_refusal(
        tool(home, "Write", file_path=str(libexec)), env_of(home)
    )
    assert found is not None
    assert found.startswith(f"{libexec} already exists")
    found = guard.first_refusal(bash(home, f"cp t.sh {libexec}"), env_of(home))
    assert found is not None
    assert found.startswith(f"{libexec}/t.sh isn't a valid name")


def test_a_name_in_another_directory_is_not_checked(home: Path, libexec: Path) -> None:
    path = libexec.with_name("tbhb2") / "run.sh"
    found = guard.first_refusal(tool(home, "Write", file_path=str(path)), env_of(home))
    assert found == outside(path)
    path = libexec.parent / "run.sh"
    found = guard.first_refusal(tool(home, "Write", file_path=str(path)), env_of(home))
    assert found == outside(path)


def test_a_non_script_still_needs_a_libexec_name(home: Path, libexec: Path) -> None:
    path = libexec / "notes.txt"
    found = guard.first_refusal(tool(home, "Write", file_path=str(path)), env_of(home))
    assert found is not None
    assert found.startswith(f"{path} isn't a valid name")


OVERWRITES = [
    "echo x > {p}",
    "echo x >> {p}",
    "echo x >| {p}",
    "echo x &> {p}",
    "echo x 2> {p}",
    "echo x | tee {p}",
    "echo x | tee -a {p}",
    "cp t.sh {p}",
    "cp t.sh {d}/",
    "cp t.sh {d}",
    "cp -t {d} t.sh",
    "mv t.sh {p}",
    "install -m755 t.sh {p}",
    "ln -sf t.sh {p}",
    "ln -sf {d}/t.sh {d}",
]


@pytest.mark.parametrize("command", OVERWRITES)
def test_writing_to_an_existing_file_is_refused(
    home: Path, libexec: Path, command: str
) -> None:
    (home / "work" / "t.sh").write_text("")
    path = libexec / NEW if "{p}" in command else libexec / "t.sh"
    path.write_text(SHEBANG)
    found = guard.first_refusal(
        bash(home, command.format(p=path, d=libexec)), env_of(home)
    )
    if command.endswith("{d}") and "-sf" in command:
        assert found is not None
        return
    assert found is not None
    assert found.startswith(
        f"{path} already exists, and a handed-over script is never edited."
    )


def test_every_tool_is_refused_on_an_existing_file(home: Path, libexec: Path) -> None:
    path = libexec / NEW
    path.write_text(SHEBANG)
    env = env_of(home)
    expected = guard.first_refusal(tool(home, "Write", file_path=str(path)), env)
    assert expected is not None
    assert expected.startswith(f"{path} already exists")
    calls: list[Json] = [
        tool(home, "Edit", file_path=str(path), old_string="a", new_string="b"),
        tool(home, "MultiEdit", file_path=str(path), edits=[]),
        tool(home, "NotebookEdit", notebook_path=str(path)),
        tool(
            home, "apply_patch", command=f"*** Begin Patch\n*** Update File: {path}\n"
        ),
        {"toolCall": {"name": "write_file", "args": {"TargetFile": str(path)}}},
    ]
    for data in calls:
        assert guard.first_refusal(data, env) == expected


def test_a_non_script_overwrite_is_refused_too(home: Path, libexec: Path) -> None:
    path = libexec / "2026-11-09-coord-foo.log"
    path.write_text("x")
    found = guard.first_refusal(tool(home, "Write", file_path=str(path)), env_of(home))
    assert found is not None
    assert found.startswith(f"{path} already exists")


def test_a_dangling_symlink_counts_as_existing(home: Path, libexec: Path) -> None:
    path = libexec / NEW
    path.symlink_to(home / "nowhere")
    found = guard.first_refusal(tool(home, "Write", file_path=str(path)), env_of(home))
    assert found is not None
    assert found.startswith(f"{path} already exists")


def test_a_symlinks_own_name_is_judged_not_its_target(
    home: Path, libexec: Path
) -> None:
    (home / ".cache").mkdir()
    link = libexec / NEW
    link.symlink_to(home / ".cache")
    found = guard.first_refusal(
        tool(home, "Write", file_path=str(link / "a.sh")), env_of(home)
    )
    assert found is not None
    assert found.startswith(f"{link}/a.sh isn't a valid name")


def test_chmod_reads_and_ls_always_pass(home: Path, libexec: Path) -> None:
    path = libexec / NEW
    path.write_text(SHEBANG)
    for command in [
        f"chmod +x {path}",
        f"chmod 755 {path} && ls {libexec}",
        f"cat {path}",
        f"bash {path}",
        f"ls -l {libexec}",
        f"cp {path} ~/.cache/",
        f"mkdir -p {libexec}/sub",
        f"touch {path}",
        f"sed -i s/a/b/ {path}",
    ]:
        found = guard.first_refusal(bash(home, command), env_of(home))
        if command == f"cp {path} ~/.cache/":
            assert found == outside(f"{home}/.cache/{NEW}")
        else:
            assert found is None, command


def test_the_migration_mv_passes(home: Path, libexec: Path) -> None:
    cache = home / ".cache" / "coord"
    cache.mkdir(parents=True)
    (cache / "x.sh").write_text(SHEBANG)
    command = f"mv ~/.cache/coord/x.sh ~/.local/libexec/tbhb/{NEW}"
    assert guard.first_refusal(bash(home, command), env_of(home)) is None
    (libexec / NEW).write_text(SHEBANG)
    found = guard.first_refusal(bash(home, command), env_of(home))
    assert found is not None
    assert found.startswith(f"{libexec}/{NEW} already exists")


def test_a_revision_goes_in_a_new_file(home: Path, libexec: Path) -> None:
    (libexec / NEW).write_text(SHEBANG)
    path = libexec / "2026-11-09-coord-foo.v2.sh"
    assert (
        guard.first_refusal(tool(home, "Write", file_path=str(path)), env_of(home))
        is None
    )


def test_the_log_directory_name_is_not_a_script_for_a_log(home: Path) -> None:
    logs = home / ".local" / "state" / "tbhb" / NEW
    command = f"echo x > {logs}/run.log"
    assert guard.first_refusal(bash(home, command), env_of(home)) is None
    assert guard.first_refusal(bash(home, f"mkdir -p {logs}"), env_of(home)) is None
    found = guard.first_refusal(bash(home, f"echo x > {logs}"), env_of(home))
    assert found == outside(logs)


# Path handling.


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("~", "{h}"),
        ("~/a", "{h}/a"),
        ("$HOME", "{h}"),
        ("$HOME/a", "{h}/a"),
        ("${HOME}", "{h}"),
        ("${HOME}/a/../b", "{h}/b"),
        ("~x/a", "{w}/~x/a"),
        ("$HOME2/a", "{w}/$HOME2/a"),
        ("${HOME}x/a", "{w}/$" + "{HOME}x/a"),
        ("a/b.sh", "{w}/a/b.sh"),
        ("./a/../b.sh", "{w}/b.sh"),
        ("../b.sh", "{h}/b.sh"),
        ("/abs/../x", "/x"),
        ("/abs//x/./y", "/abs/x/y"),
        ("~/../x", "{h}/../x"),
        ("/private/tmp/a", "/tmp/a"),
        ("/private/tmp", "/tmp"),
        ("/private/tmp2/a", "/private/tmp2/a"),
        ("/private/var/a", "/private/var/a"),
        ("/tmp/a", "/tmp/a"),
        ("/a/private/tmp/b", "/a/private/tmp/b"),
        ("/private/tmp/../a", "/private/a"),
        ("/tmp/../private/tmp/a", "/tmp/a"),
    ],
)
def test_resolve(home: Path, raw: str, expected: str) -> None:
    place = places(home)
    want = expected.replace("{h}", place.home).replace("{w}", place.cwd)
    assert place.resolve(raw) == os.path.normpath(want)


def test_normalize() -> None:
    assert guard.normalize("a/b") == "/a/b"
    assert guard.normalize("a/../..") == "/"
    assert guard.normalize("b", "/x/y") == "/x/y/b"
    assert guard.normalize("/b", "/x/y") == "/b"
    assert guard.normalize("", "/x/y") == "/x/y"
    assert guard.normalize("/private/tmp/x") == "/tmp/x"


def test_a_relative_path_resolves_against_the_payload_cwd(home: Path) -> None:
    cache = home / ".cache"
    data: Json = {
        "tool_name": "Write",
        "tool_input": {"file_path": "x/a.sh"},
        "cwd": str(cache),
    }
    assert guard.first_refusal(data, env_of(home)) == outside(cache / "x" / "a.sh")
    data["cwd"] = "/usr/local"
    assert guard.first_refusal(data, env_of(home)) is None


def test_workspace_paths_stand_in_for_cwd(home: Path) -> None:
    data: Json = {
        "toolCall": {"name": "write_file", "args": {"Path": "a.sh"}},
        "workspacePaths": [str(home / ".cache")],
    }
    assert guard.first_refusal(data, env_of(home)) == outside(home / ".cache" / "a.sh")


def test_without_a_cwd_the_process_cwd_is_used(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(home)
    data: Json = {"tool_name": "Write", "tool_input": {"file_path": ".cache/a.sh"}}
    found = guard.first_refusal(data, env_of(home))
    assert found == outside(Path.cwd() / ".cache" / "a.sh")


def test_home_comes_from_the_environment_given(home: Path) -> None:
    data = tool(home, "Write", file_path="~/x.sh")
    assert guard.first_refusal(data, env_of(home)) == outside(home / "x.sh")
    assert guard.first_refusal(data, env_of(home / "work")) == outside(
        home / "work" / "x.sh"
    )


# Payload shapes.


def patch(*lines: str) -> str:
    return "\n".join(["*** Begin Patch", *lines, "*** End Patch"])


@pytest.mark.parametrize("field", ["command", "patch", "input"])
def test_apply_patch_add_file(home: Path, field: str) -> None:
    text = patch("*** Add File: ~/.cache/a.sh", "+echo x")
    data = tool(home, "apply_patch", **{field: text})
    assert guard.first_refusal(data, env_of(home)) == outside(home / ".cache" / "a.sh")


def test_apply_patch_paths(home: Path) -> None:
    place = places(home)
    text = patch(
        "*** Add File: a.sh",
        "+echo",
        "*** Add File: shebang",
        "+#!/bin/sh",
        "*** Add File: second",
        "+echo",
        "+#!/bin/sh",
        "*** Add File: empty",
        "*** Add File: last",
        "*** Update File: u.sh",
        "@@",
        "-a",
        "+b",
        "*** Update File: v",
        "*** Move to: moved",
        "*** Update File: w.sh",
        "*** Move to: moved.sh",
        "*** Update File: x",
        "*** Move to: y.sh",
        "*** Update File: m.sh",
        "*** Move to: m2",
        "*** Add File: odd: name.sh",
        "*** Delete File: gone.sh",
    )
    w = str(home / "work")
    assert guard.patch_writes(text, place) == [
        guard.Write(f"{w}/a.sh", script=True),
        guard.Write(f"{w}/shebang", script=True),
        guard.Write(f"{w}/second", script=False),
        guard.Write(f"{w}/empty", script=False),
        guard.Write(f"{w}/last", script=False),
        guard.Write(f"{w}/u.sh", script=True),
        guard.Write(f"{w}/v", script=False),
        guard.Write(f"{w}/moved", script=False),
        guard.Write(f"{w}/w.sh", script=True),
        guard.Write(f"{w}/moved.sh", script=True),
        guard.Write(f"{w}/x", script=False),
        guard.Write(f"{w}/y.sh", script=True),
        guard.Write(f"{w}/m.sh", script=True),
        guard.Write(f"{w}/m2", script=True),
        guard.Write(f"{w}/odd: name.sh", script=True),
    ]
    assert guard.patch_writes("", place) == []
    assert guard.patch_writes("*** Move to: z", place) == []
    assert guard.patch_writes("+*** Add File: a.sh", place) == []


def test_apply_patch_moves_a_script_into_the_cache(home: Path) -> None:
    text = patch("*** Update File: /usr/local/t.sh", "*** Move to: ~/.cache/t")
    data = tool(home, "apply_patch", command=text)
    assert guard.first_refusal(data, env_of(home)) == outside(home / ".cache" / "t")


def test_apply_patch_adding_a_shebang_file(home: Path) -> None:
    text = patch("*** Add File: ~/.cache/run", "+#!/bin/sh", "+echo")
    data = tool(home, "apply_patch", command=text)
    assert guard.first_refusal(data, env_of(home)) == outside(home / ".cache" / "run")
    other = patch("*** Add File: ~/.cache/run", "+echo")
    assert (
        guard.first_refusal(tool(home, "apply_patch", command=other), env_of(home))
        is None
    )
    assert guard.first_refusal(tool(home, "apply_patch"), env_of(home)) is None
    assert (
        guard.first_refusal(tool(home, "apply_patch", command=5), env_of(home)) is None
    )


def test_apply_patch_names_the_first_refused_file(home: Path, libexec: Path) -> None:
    text = patch(
        f"*** Add File: {libexec}/{NEW}", "+x", "*** Add File: ~/.cache/a.sh", "+x"
    )
    data = tool(home, "apply_patch", command=text)
    assert guard.first_refusal(data, env_of(home)) == outside(home / ".cache" / "a.sh")


@pytest.mark.parametrize("name", ["Write", "Edit", "MultiEdit"])
def test_claude_file_tools(home: Path, name: str) -> None:
    data = tool(home, name, file_path="~/.cache/a.sh")
    assert guard.first_refusal(data, env_of(home)) == outside(home / ".cache" / "a.sh")
    data = tool(home, name, file_path="~/.cache/a.txt")
    assert guard.first_refusal(data, env_of(home)) is None


def test_claude_notebook_edit(home: Path) -> None:
    data = tool(home, "NotebookEdit", notebook_path="~/.cache/a.sh")
    assert guard.first_refusal(data, env_of(home)) == outside(home / ".cache" / "a.sh")
    data = tool(home, "NotebookEdit", notebook_path="~/.cache/a.ipynb")
    assert guard.first_refusal(data, env_of(home)) is None
    data = tool(home, "NotebookEdit", notebook_path=3, file_path="~/.cache/a.sh")
    assert guard.first_refusal(data, env_of(home)) == outside(home / ".cache" / "a.sh")


@pytest.mark.parametrize(
    "key",
    [
        "Path",
        "TargetFile",
        "AbsolutePath",
        "target_file",
        "file_path",
        "filePath",
        "path",
    ],
)
def test_agy_write_file_path_keys(home: Path, key: str) -> None:
    for args in ({key: "~/.cache/a.sh"}, json.dumps({key: "~/.cache/a.sh"})):
        data: Json = {
            "toolCall": {"name": "write_file", "args": args},
            "cwd": str(home),
        }
        found = guard.first_refusal(data, env_of(home))
        assert found == outside(home / ".cache" / "a.sh")


def test_agy_arguments_that_are_not_json_or_an_object(home: Path) -> None:
    bad: list[object] = ["not json", "[]", "5", None, [], 7, ""]
    for args in bad:
        data: Json = {"toolCall": {"name": "write_file", "args": args}}
        assert guard.first_refusal(data, env_of(home)) is None
    data: Json = {
        "toolName": "write_file",
        "toolInput": {"Path": "~/.cache/a.sh"},
        "toolCall": {},
    }
    assert guard.first_refusal(data, env_of(home)) == outside(home / ".cache" / "a.sh")


def test_agy_run_command(home: Path) -> None:
    data: Json = {
        "toolCall": {
            "name": "run_command",
            "args": {"CommandLine": "echo x > ~/.cache/a.sh"},
        }
    }
    assert guard.first_refusal(data, env_of(home)) == outside(home / ".cache" / "a.sh")


def test_a_payload_with_neither_a_command_nor_a_path_is_allowed(home: Path) -> None:
    cases: list[Json] = [
        {},
        {"tool_input": {}},
        {"tool_input": "x"},
        {"tool_name": "Read", "tool_input": {"file_path": "~/.cache/a.sh", "limit": 5}},
        {"toolCall": 5},
        {"tool_input": {"command": 5}},
        {"tool_input": {"file_path": ""}},
        {"tool_input": {"file_path": 5}},
    ]
    for data in cases:
        assert guard.first_refusal(data, env_of(home)) is None, data


def test_tool_call_shapes() -> None:
    assert guard.tool_call({"tool_name": "Write", "tool_input": {"a": 1}}) == (
        "Write",
        {"a": 1},
    )
    assert guard.tool_call({"tool_name": 5, "tool_input": []}) == (5, {})
    assert guard.tool_call({"toolCall": {"name": "n", "args": '{"a": 1}'}}) == (
        "n",
        {"a": 1},
    )
    assert guard.tool_call({"toolCall": {"name": "n", "args": {"a": 1}}}) == (
        "n",
        {"a": 1},
    )
    assert guard.tool_call({"toolCall": {"name": "n", "args": "{"}}) == ("n", {})
    assert guard.tool_call(
        {"toolCall": 3, "tool_name": "x", "tool_input": {"b": 2}}
    ) == ("x", {"b": 2})


def test_first_str() -> None:
    assert guard.first_str({"a": 1, "b": "x", "c": "y"}, ("a", "b", "c")) == "x"
    assert guard.first_str({"a": ""}, ("a", "b")) == ""
    assert guard.first_str({}, ("a",)) == ""
    assert guard.first_str({"b": "z"}, ("a", "b")) == "z"


def test_valid_name_unit() -> None:
    assert guard.valid_name(NEW)
    assert guard.valid_name("2024-02-29-a-b.v1.sh")
    assert not guard.valid_name("2025-02-29-a-b.sh")
    assert not guard.valid_name("2026-11-09-ab.sh")
    assert not guard.valid_name("2026-11-31-a-b.sh")
    assert not guard.valid_name("2026-12-01-a-b.sh\n")


def test_under() -> None:
    assert guard.under("/a/b", "/a")
    assert guard.under("/a", "/a")
    assert not guard.under("/ab", "/a")
    assert not guard.under("/a", "/a/b")
    assert guard.under("/a/b", "/a/")
    assert not guard.under("/", "/a")
    assert guard.under("/a", "/")
    assert guard.under("/aX/b", "/aX")
    assert not guard.under("/a/b", "/aX")


def test_copy_writes(home: Path) -> None:
    place = places(home)
    w = str(home / "work")
    cases: list[tuple[str, list[str], list[guard.Write]]] = [
        ("cp", ["a", "b"], [guard.Write(f"{w}/b", script=False)]),
        ("cp", ["a.sh", "b"], [guard.Write(f"{w}/b", script=True)]),
        ("cp", ["a", "b.sh"], [guard.Write(f"{w}/b.sh", script=True)]),
        ("cp", ["a"], []),
        ("cp", [], []),
        ("cp", ["-r", "a"], []),
        (
            "cp",
            ["a", "b", "d/"],
            [
                guard.Write(f"{w}/d/a", script=False),
                guard.Write(f"{w}/d/b", script=False),
            ],
        ),
        ("cp", ["x/a.sh", "d/"], [guard.Write(f"{w}/d/a.sh", script=True)]),
        ("cp", ["-t", "d", "a"], [guard.Write(f"{w}/d/a", script=False)]),
        ("cp", ["-t", "d"], []),
        (
            "cp",
            ["--target-directory=d", "a", "b"],
            [
                guard.Write(f"{w}/d/a", script=False),
                guard.Write(f"{w}/d/b", script=False),
            ],
        ),
        ("ln", ["-s", "x/a.sh"], [guard.Write(f"{w}/a.sh", script=True)]),
        ("ln", ["-s", "a", "b"], [guard.Write(f"{w}/b", script=False)]),
        (
            "ln",
            ["-s", "a", "b", "c"],
            [
                guard.Write(f"{w}/c/a", script=False),
                guard.Write(f"{w}/c/b", script=False),
            ],
        ),
        ("mv", ["a", "b"], [guard.Write(f"{w}/b", script=False)]),
        ("install", ["-m", "755", "a", "b"], [guard.Write(f"{w}/b", script=False)]),
        ("install", ["-m755", "a", "b"], [guard.Write(f"{w}/b", script=False)]),
        ("install", ["-d", "a", "b"], []),
        ("install", ["--directory", "a"], []),
        ("install", ["--directory", "a", "b"], []),
        ("install", ["-D", "a", "b"], [guard.Write(f"{w}/b", script=False)]),
        ("cp", ["-S", ".bak", "a", "b"], [guard.Write(f"{w}/b", script=False)]),
        ("cp", ["--suffix=.bak", "a", "b"], [guard.Write(f"{w}/b", script=False)]),
    ]
    for program, args, expected in cases:
        assert list(guard.copy_writes(program, args, place)) == expected, (
            program,
            args,
        )


def test_an_existing_directory_destination_takes_the_source_name(home: Path) -> None:
    (home / "work" / "d").mkdir()
    place = places(home)
    found = list(guard.copy_writes("cp", ["x.sh", "d"], place))
    assert found == [guard.Write(f"{home}/work/d/x.sh", script=True)]
    found = list(guard.copy_writes("cp", ["x.sh", "e"], place))
    assert found == [guard.Write(f"{home}/work/e", script=True)]
    link = home / "work" / "l"
    link.symlink_to(home / "work" / "d")
    found = list(guard.copy_writes("cp", ["x.sh", "l"], place))
    assert found == [guard.Write(f"{home}/work/l/x.sh", script=True)]


def test_command_writes(home: Path) -> None:
    place = places(home)
    w = str(home / "work")
    cases: list[tuple[list[str], str | None, list[guard.Write]]] = [
        (
            ["echo", guard.Redirect(">"), "f.sh"],
            None,
            [guard.Write(f"{w}/f.sh", script=True)],
        ),
        (
            ["echo", guard.Redirect(">"), "f"],
            "#!/bin/sh\n",
            [guard.Write(f"{w}/f", script=True)],
        ),
        (
            ["echo", guard.Redirect(">"), "f"],
            "echo\n",
            [guard.Write(f"{w}/f", script=False)],
        ),
        (
            ["tee", "-a", "f", "g.sh"],
            "#!x\n",
            [guard.Write(f"{w}/f", script=True), guard.Write(f"{w}/g.sh", script=True)],
        ),
        (["tee", "f"], "#!x\n", [guard.Write(f"{w}/f", script=True)]),
        (["tee", "f"], "x\n", [guard.Write(f"{w}/f", script=False)]),
        (["sudo", "-n", "tee", "f.sh"], None, [guard.Write(f"{w}/f.sh", script=True)]),
        (["A=1", "tee", "f.sh"], None, [guard.Write(f"{w}/f.sh", script=True)]),
        (["/usr/bin/tee", "f.sh"], None, [guard.Write(f"{w}/f.sh", script=True)]),
        (["bash", "-c", "echo > f.sh"], None, [guard.Write(f"{w}/f.sh", script=True)]),
        (["eval", "echo", ">", "f.sh"], None, [guard.Write(f"{w}/f.sh", script=True)]),
        (["ssh", "h", "echo > f.sh"], None, []),
        (["/usr/bin/ssh", "h", "echo > f.sh"], None, []),
        (["cat", "f.sh"], None, []),
        ([], None, []),
        (["A=1"], None, []),
    ]
    for command, body, expected in cases:
        assert list(guard.command_writes(command, body, place)) == expected, command


def test_bash_writes_pairs_each_heredoc_with_its_command(home: Path) -> None:
    place = places(home)
    w = str(home / "work")
    text = "cat > a <<E\n#!/bin/sh\nE\ncat > b <<E\nx\nE\ncat > c\n"
    assert list(guard.bash_writes(text, place)) == [
        guard.Write(f"{w}/a", script=True),
        guard.Write(f"{w}/b", script=False),
        guard.Write(f"{w}/c", script=False),
    ]


def test_targets(home: Path) -> None:
    place = places(home)
    w = str(home / "work")
    assert guard.targets(bash(home, "echo > a.sh"), place) == [
        guard.Write(f"{w}/a.sh", script=True)
    ]
    assert guard.targets(tool(home, "Write", file_path="a"), place) == [
        guard.Write(f"{w}/a", script=False)
    ]
    assert guard.targets(tool(home, "Write", file_path="a", content="#!"), place) == [
        guard.Write(f"{w}/a", script=True)
    ]
    assert guard.targets({}, place) == []
    # A patch's text is not a command.
    data = tool(home, "apply_patch", command="*** Add File: a.sh\n+x")
    assert guard.targets(data, place) == [guard.Write(f"{w}/a.sh", script=True)]


def test_first_refusal_returns_the_first_of_several(home: Path, libexec: Path) -> None:
    command = f"echo x > ~/.cache/a.sh && echo x > {libexec}/bad && echo > /tmp/b.sh"
    assert guard.first_refusal(bash(home, command), env_of(home)) == outside(
        home / ".cache" / "a.sh"
    )
    command = f"echo x > ~/work/ok.txt && echo > {libexec}/run.sh && echo > /tmp/b.sh"
    found = guard.first_refusal(bash(home, command), env_of(home))
    assert found is not None
    assert found.startswith(f"{libexec}/run.sh isn't")
    command = "echo > /usr/ok.sh && echo > /tmp/b.sh"
    assert guard.first_refusal(bash(home, command), env_of(home)) == outside(
        "/tmp/b.sh"
    )


def test_a_missing_home_fails_open(
    home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("HOME", raising=False)
    payload = json.dumps(tool(home, "Write", file_path="/tmp/a.sh"))
    monkeypatch.setattr(sys, "stdin", io.StringIO(payload))
    assert guard.main() == 0
    assert capsys.readouterr() == ("", "")


# A leading `//` names the same directory as `/`.


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("//a", "/a"),
        ("///a", "/a"),
        ("//", "/"),
        ("//private/tmp/a", "/tmp/a"),
        ("//a//b", "/a/b"),
        ("/Xa", "/Xa"),
        ("//Xa", "/Xa"),
    ],
)
def test_leading_slashes_collapse(raw: str, expected: str) -> None:
    assert guard.normalize(raw) == expected
    assert guard.normalize(raw, "/x") == expected


def test_a_double_slash_does_not_skip_the_tmp_check(home: Path) -> None:
    for command in ("echo x > //tmp/a.sh", "echo x > ///tmp/a.sh"):
        found = guard.first_refusal(bash(home, command), env_of(home))
        assert found == outside("/tmp/a.sh")
    found = guard.first_refusal(bash(home, "echo x > //private/tmp/a.sh"), env_of(home))
    assert found == outside("/tmp/a.sh")


def test_a_double_slash_does_not_skip_the_home_check(home: Path) -> None:
    found = guard.first_refusal(
        bash(home, f"echo x > /{home}/.cache/a.sh"), env_of(home)
    )
    assert found == outside(f"{home}/.cache/a.sh")


def test_a_double_slash_does_not_skip_the_libexec_check(
    home: Path, libexec: Path
) -> None:
    (libexec / NEW).write_text(SHEBANG)
    found = guard.first_refusal(bash(home, f"echo x > /{libexec}/{NEW}"), env_of(home))
    assert found is not None
    assert found.startswith(f"{libexec}/{NEW} already exists")
    found = guard.first_refusal(bash(home, f"echo x > /{libexec}/run.sh"), env_of(home))
    assert found is not None
    assert found.startswith(f"{libexec}/run.sh isn't a valid name")


# A cd earlier in the command moves where later segments write.

WRITE = "echo x > a.sh"


@pytest.fixture
def repo(home: Path) -> Path:
    path = home / "Code" / "repo"
    (path / ".git").mkdir(parents=True)
    (path / "sub").mkdir()
    return path


@pytest.mark.parametrize(
    ("command", "written"),
    [
        # refused where the cd leaves it, relative to the home directory
        ("cd ~/.cache && {w}", ".cache/a.sh"),
        ("cd $HOME/.cache; {w}", ".cache/a.sh"),
        ("cd ${HOME}/.cache || {w}", ".cache/a.sh"),
        ("cd ~/.cache\n{w}", ".cache/a.sh"),
        ("cd ~/.cache > /dev/null && {w}", ".cache/a.sh"),
        ("cd && {w}", "a.sh"),
        ("cd .. && {w}", "a.sh"),
        ("cd .cache && {w}", "work/.cache/a.sh"),
        ("cd ~/Code/repo && cd ~/.cache && {w}", ".cache/a.sh"),
        ("cd ~/.cache && cd .. && {w}", "a.sh"),
        ("echo $(cd ~/.cache; {w})", ".cache/a.sh"),
        ("bash -c 'cd ~/.cache; {w}'", ".cache/a.sh"),
        ("cd ~/.cache && bash -c '{w}'", ".cache/a.sh"),
        # a cd in a ( ) group or a substitution is over when it closes
        ("(cd ~/.cache) && {w}", "work/a.sh"),
        ("(cd ~/Code/repo) && {w}", "work/a.sh"),
        ("echo $(cd ~/Code/repo) && {w}", "work/a.sh"),
        ("echo `cd ~/Code/repo` && {w}", "work/a.sh"),
        # a cd that can't be followed leaves the directory the text began in
        ("cd - && {w}", "work/a.sh"),
        ("cd ~/Code/repo && cd - && {w}", "work/a.sh"),
        ("cd ~/Code/repo && cd -P && {w}", "work/a.sh"),
        ("cd ~/Code/repo && cd $ELSEWHERE && {w}", "work/a.sh"),
        ("cd ~/Code/repo && cd ${ELSEWHERE} && {w}", "work/a.sh"),
        ("cd ~/Code/repo && cd $(mktemp -d) && {w}", "work/a.sh"),
        ('cd ~/Code/repo && cd "$(mktemp -d)" && {w}', "work/a.sh"),
        ("cd ~/Code/repo && cd `mktemp -d` && {w}", "work/a.sh"),
        ("cd ~/Code/repo && cd ~other && {w}", "work/a.sh"),
        ("cd ~/Code/repo && cd -P ~/Code/repo && {w}", "work/a.sh"),
        ("cd ~/Code/repo && cd a b && {w}", "work/a.sh"),
        # allowed: a work tree, as tracked, whatever the cwd says
        ("cd ~/Code/repo && {w}", None),
        ("cd ~/Code/repo; {w}", None),
        ("cd ~/Code/repo > /dev/null && {w}", None),
        ("builtin cd ~/Code/repo && {w}", None),
        ("cd ~/Code/repo && cd sub && {w}", None),
        ("cd ~/Code/repo && (cd ~/.cache; true) && {w}", None),
        ("cd ~/Code/repo && ({w})", None),
        ("cd ~/Code/repo && echo $(true) && {w}", None),
        ("echo $(true) && cd ~/Code/repo && {w}", None),
        ("bash -c 'cd ~/Code/repo && {w}'", None),
        ("cd ~/Code/repo && bash -c '{w}'", None),
        ("cd ~/Code/repo && echo x > a.txt", None),
    ],
)
def test_a_cd_moves_later_writes(
    home: Path, repo: Path, command: str, written: str | None
) -> None:
    assert repo.is_dir()
    found = guard.first_refusal(
        bash(home, command.replace("{w}", WRITE)), env_of(home, "/tmp")
    )
    assert found == (None if written is None else outside(f"{home}/{written}"))


def test_a_cd_then_a_heredoc_script_is_judged_where_it_lands(
    home: Path, repo: Path
) -> None:
    body = "a.sh <<'E'\n#!/bin/sh\nE\n"
    found = guard.first_refusal(
        bash(home, f"cd ~/.cache && cat > {body}"), env_of(home)
    )
    assert found == outside(f"{home}/.cache/a.sh")
    assert (
        guard.first_refusal(bash(home, f"cd {repo} && cat > {body}"), env_of(home))
        is None
    )
    plain = "cd ~/.cache && cat > a <<'E'\n#!/bin/sh\nE\n"
    assert guard.first_refusal(bash(home, plain), env_of(home)) == outside(
        f"{home}/.cache/a"
    )


def test_a_cd_to_an_unset_tmpdir_leaves_the_start(home: Path, repo: Path) -> None:
    command = "cd ~/Code/repo && cd $TMPDIR && echo x > a.sh"
    assert repo.is_dir()
    found = guard.first_refusal(bash(home, command), env_of(home))
    assert found == outside(f"{home}/work/a.sh")


def test_a_cd_tracked_to_a_run_scoped_tmpdir(home: Path) -> None:
    command = "cd $TMPDIR && echo x > a.sh"
    assert guard.first_refusal(bash(home, command), env_of(home, "/tmp/run-1")) is None
    assert guard.first_refusal(bash(home, command), env_of(home, "/tmp")) == outside(
        "/tmp/a.sh"
    )


# $TMPDIR in a path.


def test_a_tmpdir_in_a_path_expands(home: Path) -> None:
    for command in (
        'cat > "$TMPDIR/run.sh"',
        "cat > ${TMPDIR}/run.sh",
        "cp t.sh $TMPDIR/run.sh",
        "echo x | tee $TMPDIR/run.sh",
    ):
        (home / "work" / "t.sh").write_text("")
        found = guard.first_refusal(bash(home, command), env_of(home, "/tmp/run-1"))
        assert found is None, command
        found = guard.first_refusal(bash(home, command), env_of(home, "/tmp"))
        assert found == outside("/tmp/run.sh"), command
    data = tool(home, "Write", file_path="$TMPDIR/run.sh")
    assert guard.first_refusal(data, env_of(home, "/tmp/run-1")) is None
    assert guard.first_refusal(data, env_of(home, "/tmp")) == outside("/tmp/run.sh")


@pytest.mark.parametrize("tmpdir", [None, ""])
def test_an_unset_tmpdir_leaves_the_path_literal(
    home: Path, tmpdir: str | None
) -> None:
    work = home / "work"
    for raw in ("$TMPDIR", "${TMPDIR}"):
        command = f'cat > "{raw}/run.sh"'
        found = guard.first_refusal(bash(home, command), env_of(home, tmpdir))
        assert found == outside(f"{work}/{raw}/run.sh")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("$TMPDIR", "/tmp/run-1"),
        ("$TMPDIR/a", "/tmp/run-1/a"),
        ("${TMPDIR}", "/tmp/run-1"),
        ("${TMPDIR}/a/../b", "/tmp/run-1/b"),
        ("$TMPDIR2/a", "{w}/$TMPDIR2/a"),
        ("${TMPDIR}x/a", "{w}/$" + "{TMPDIR}x/a"),
        ("x/$TMPDIR", "{w}/x/$TMPDIR"),
        ("$HOME/a", "{h}/a"),
    ],
)
def test_resolve_expands_tmpdir(home: Path, raw: str, expected: str) -> None:
    place = places(home, "/tmp/run-1")
    want = expected.replace("{h}", place.home).replace("{w}", place.cwd)
    assert place.resolve(raw) == os.path.normpath(want)


def test_resolve_leaves_an_empty_tmpdir_literal(home: Path) -> None:
    place = places(home, "")
    assert place.resolve("$TMPDIR/a") == f"{place.cwd}/$TMPDIR/a"
    assert place.resolve("${TMPDIR}") == f"{place.cwd}/$" + "{TMPDIR}"


# The hook.


def run_hook(
    monkeypatch: pytest.MonkeyPatch,
    home: Path,
    payload: object,
    *,
    tmpdir: str | None = None,
) -> int:
    monkeypatch.setenv("HOME", str(home))
    if tmpdir is None:
        monkeypatch.delenv("TMPDIR", raising=False)
    else:
        monkeypatch.setenv("TMPDIR", tmpdir)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    return alpha.main(["guard-scripts"], "tbhbdev alpha")


@pytest.mark.parametrize("agy", [False, True])
def test_hook_contract(
    home: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    *,
    agy: bool,
) -> None:
    command = "echo x > ~/.cache/a.sh"
    payload = (
        {"toolCall": {"name": "run_command", "args": {"CommandLine": command}}}
        if agy
        else bash(home, command)
    )
    assert run_hook(monkeypatch, home, payload) == 0
    output = json.loads(capsys.readouterr().out)
    reason = outside(home / ".cache" / "a.sh")
    if agy:
        assert output == {"decision": "deny", "reason": reason}
    else:
        assert list(output) == ["hookSpecificOutput"]
        inner = output["hookSpecificOutput"]
        assert inner["hookEventName"] == "PreToolUse"
        assert inner["permissionDecision"] == "deny"
        assert inner["permissionDecisionReason"] == reason


def test_hook_contract_for_an_agy_write_file(
    home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    payload = {"toolCall": {"name": "write_file", "args": {"Path": "~/.cache/a.sh"}}}
    assert run_hook(monkeypatch, home, payload) == 0
    output = json.loads(capsys.readouterr().out)
    assert output == {"decision": "deny", "reason": outside(home / ".cache" / "a.sh")}


def test_hook_contract_for_a_codex_patch(
    home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    payload = tool(
        home, "apply_patch", command=patch("*** Add File: ~/.cache/a.sh", "+x")
    )
    assert run_hook(monkeypatch, home, payload) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_hook_reads_tmpdir_from_the_environment(
    home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    payload = tool(home, "Write", file_path="/tmp/run-1/a.sh")
    assert run_hook(monkeypatch, home, payload, tmpdir="/tmp/run-1") == 0
    assert capsys.readouterr() == ("", "")
    assert run_hook(monkeypatch, home, payload) == 0
    assert "deny" in capsys.readouterr().out


@pytest.mark.parametrize(
    "payload",
    ["invalid", "[]", "{}", '{"tool_input":{"command":"true"}}', ""],
)
def test_hook_allows_unreadable_or_allowed_input(
    payload: str,
    home: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(sys, "stdin", io.StringIO(payload))
    status = alpha.main(["guard-scripts"], "tbhbdev alpha")
    assert (status, capsys.readouterr().out) == (0, "")


def test_hook_crashes_fail_open(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def crash(_data: Json, _env: Mapping[str, str]) -> None:
        raise RuntimeError

    monkeypatch.setattr(guard, "first_refusal", crash)
    monkeypatch.setattr(sys, "stdin", io.StringIO('{"tool_input": {}}'))
    status = alpha.main(["guard-scripts"], "tbhbdev alpha")
    assert (status, capsys.readouterr().out) == (0, "")


def test_deep_nesting_fails_open(
    home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    command = "eval " * 1000 + "echo > ~/.cache/a.sh"
    assert run_hook(monkeypatch, home, bash(home, command)) == 0
    assert capsys.readouterr().out == ""


def test_the_command_is_registered() -> None:
    assert alpha.COMMANDS["guard-scripts"] is alpha.scripts_command
    assert "guard-scripts" in alpha.USAGE

# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns
"""Tests for tbhbdev alpha guard-gh-poll, which shares guard-gh-pr's scanner."""

import io
import itertools
import json
import os
import shlex
import sys
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING

import pytest

from tbhbdev import alpha
from tbhbdev.alpha import guard_gh_poll as guard

if TYPE_CHECKING:
    from pathlib import Path

SHA = "abcdef0123456789abcdef0123456789abcdef01"
# The 1Password service account token's variable, split for gitleaks.
TOKEN = "OP_SERVICE_" + "ACCOUNT_TOKEN"
FIELDS = (
    "databaseId,event,conclusion,createdAt,startedAt,updatedAt,"
    "workflowName,attempt,headBranch"
)


REPOS = ["example/one", "example/two"]


def listing(repo: str) -> str:
    return f"gh run list -R {repo} --limit 100 --json {FIELDS}"


def today() -> date:
    return datetime.now().astimezone().date()


@pytest.fixture(autouse=True)
def grant_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "state" / "allowance.json"
    path.parent.mkdir()
    path.write_text(json.dumps({"repos": REPOS, "expires": date.max.isoformat()}))
    monkeypatch.setattr(guard, "ALLOWANCE_GRANT", path)
    monkeypatch.setattr(guard, "ALLOWANCE_COUNT", path.parent / "allowance.count")
    return path


@pytest.mark.parametrize(
    "command",
    [
        # watch and list commands
        "gh run watch 42",
        "gh run list",
        "gh pr checks --watch",
        "gh pr checks 3 --watch=true",
        "gh -R org/repo run list",
        "gh --repo=org/repo run list",
        "gh --hostname=example.com -R org/repo run list",
        "gh -R org/repo pr checks 1 --watch",
        "/opt/bin/gh --hostname example.com run watch",
        # pagination, on any endpoint and with any method
        "gh query --paginate rate_limit",
        "gh api rate_limit --paginate=true",
        "gh api --paginate repos/o/r/actions/runs/1",
        "gh api --method POST --paginate repos/o/r/actions/runs",
        "gh api --input body.json --paginate x",
        "gh api graphql --paginate -f query=x",
        # list endpoints
        "gh api repos/o/r/actions/runs",
        "gh api repos/o/r/actions/runs/",
        "gh api repos/{owner}/{repo}/actions/runs",
        "gh api /repos/o/r/actions/jobs?per_page=2",
        "gh query repos/o/r/check-runs",
        "gh api https://api.github.com/repos/o/r/check-suites/",
        "gh api https://ghe.example/api/v3/repos/o/r/actions/runs",
        "gh api repos/o/r/actions/workflows/build.yml/runs",
        f"gh api repos/o/r/actions/workflows/build.yml/runs?head_sha={SHA}",
        "gh api repos/o/r/actions/runs?head_sha=abc",
        f"gh api repos/o/r/actions/runs?head_sha={SHA[:-1]}",
        f"gh api repos/o/r/actions/runs?head_sha={SHA} --paginate",
        f"gh api 'repos/o/r/actions/runs?head_sha={SHA}&head_sha={SHA}'",
        "gh api rate_limit repos/o/r/actions/runs",
        "gh api -X get repos/o/r/actions/runs",
        "gh api --method=HEAD repos/o/r/check-runs",
        "gh api -F status=queued -X GET repos/o/r/actions/runs",
        # prefixes
        "A=1 gh run list",
        "sudo -E gh run list",
        "env -C /work gh run list",
        "env --chdir=/work -S 'gh run list'",
        "env -u OTHER gh run list",
        "env --unset OTHER gh run list",
        # loops
        "while true; do gh api rate_limit; done",
        "until gh pr view 1; do echo waiting; done",
        "for id in 1 2; do gh api repos/o/r/pulls/$id; done",
        "while pgrep -f fetch.py; do sleep 5; done",
        "while pgrep -f fetch.py; do sleep 5",
        "until sleep 1; do /usr/bin/pgrep fetch; done",
        "for x in a; do for y in b; do echo x; done; gh pr view; done",
        "{ while true; do gh pr view; done; }",
        "if true; then while true; do gh pr view; done; fi",
        "while true\ndo\n  gh pr view 1\n  sleep 30\ndone",
        "while true; do sleep 5; pgrep fetch; done",
        "for x in a; do eval 'gh pr view'; done",
        "while true; do ssh host 'gh pr view'; done",
        "while true; do bash -c 'gh api rate_limit'; done",
        "while true; do sh <<EOF\ngh pr view\nEOF\ndone",
        # nested scripts
        "bash -c 'while true; do gh pr view; done'",
        "zsh -lc 'gh run list'",
        "bash --noprofile -lc 'gh run list'",
        "eval 'gh run list'",
        "eval gh run list",
        "echo $(gh run list)",
        'echo "`gh run list`"',
        "ssh -p 22 host 'gh run list'",
        "ssh -t host gh run list",
        "ssh -o BatchMode=yes host 'while pgrep x; do sleep 1; done'",
        # heredoc bodies a shell or ssh runs
        "bash <<EOF\ngh run list\nEOF",
        "bash -s <<'EOF'\nwhile pgrep -f fetch.py; do sleep 5; done\nEOF",
        "sh -x <<-EOF\n\tgh run list\n\tEOF",
        "ssh host <<EOF\ngh run list\nEOF",
        "cat <<A; bash <<B\nhello\nA\ngh run list\nB",
        "cat <<EOF\ngh api rate_limit\nEOF\ngh run list",
    ],
)
def test_denies_polling(command: str) -> None:
    assert guard.inspect(command)[0] == guard.POLL_REASON


@pytest.mark.parametrize(
    "command",
    [
        "",
        "true",
        "gh",
        "gh api",
        "gh run",
        "gh run view 42",
        "gh run view 42 --log-failed",
        "gh pr checks",
        "gh pr checks 3",
        "gh pr checks --watchful",
        "gh pr view 1",
        "gh api repos/o/r/actions/runs/42",
        "gh api repos/o/r/actions/runs/42/",
        "gh api repos/o/r/actions/runs/42/jobs",
        "gh api repos/o/r/actions/jobs/123",
        "gh api repos/o/r/check-runs/1",
        "gh api repos/o/r/check-suites/1",
        "gh api repos/o/r/check-suites/X",
        "gh api repos/o/r/pulls/1",
        f"gh api repos/o/r/commits/{SHA}/check-runs",
        f"gh api repos/o/r/actions/runs?head_sha={SHA}",
        f"gh api 'repos/o/r/actions/runs?head_sha={SHA}&per_page=5'",
        "gh api rate_limit",
        "gh query rate_limit",
        "gh api -X POST repos/o/r/actions/runs",
        "gh api --method=PATCH repos/o/r/check-runs",
        "gh api -f name=x repos/o/r/actions/runs",
        "gh api --input body.json repos/o/r/check-runs",
        "gh pr list",
        # quoted text and heredoc bodies are data
        "echo 'gh run list'",
        "git commit -m 'while gh run list'",
        "cat <<'EOF'\nwhile pgrep -f fetch.py; do sleep 5; done\nEOF",
        "cat <<EOF\ngh run list\nEOF",
        "bash script.sh <<EOF\ngh run list\nEOF",
        "bash -c 'echo hi' <<EOF\ngh run list\nEOF",
        "ssh host 'echo hi' <<EOF\ngh run list\nEOF",
        # loops that neither run gh nor wait on pgrep
        "for x in a; do echo gh sleep pgrep; done",
        "while true; do sleep 5; done",
        "while pgrep fetch; do echo waiting; done",
        "for x in a; do true; done; gh pr view",
        "while true; do true; done; pgrep fetch; sleep 5",
        "while true; do for x in a; do true; done; sleep 1; done; pgrep x",
        "echo while; gh pr view",
        "echo for; sleep 1; pgrep x",
        # shells, eval and ssh without a polling script
        "bash script.sh",
        "bash --noprofile -lc 'echo gh'",
        "eval 'echo gh'",
        "ssh host",
        "ssh -p 22",
        "ssh -p 22 host",
        "env -u OTHER gh api rate_limit",
        "env --unset=OTHER true",
        "command -- /bin/true",
        "env -u OTHER env -- true",
        "env -u",
        "do",
        "done",
    ],
)
def test_allows_object_reads_and_data(command: str) -> None:
    assert guard.inspect(command)[0] is None


UNSETS = [
    f"unset {TOKEN}",
    f"unset -v OTHER {TOKEN}",
    f"env -u {TOKEN} op whoami",
    f"env --unset {TOKEN} op whoami",
    f"env --unset={TOKEN} op whoami",
    f"env -u{TOKEN} op whoami",
]
WRAPS = ["{}", "bash -c '{}'", "zsh -lc '{}'", "eval '{}'", "ssh host '{}'"]


@pytest.mark.parametrize(
    "command",
    [
        *(wrap.format(unset) for wrap, unset in itertools.product(WRAPS, UNSETS)),
        f"builtin unset -v {TOKEN}",
        f"A=x command env --unset={TOKEN} op whoami",
        f"/usr/bin/env -u {TOKEN} true",
        f"env -i -u {TOKEN} true",
        f"env -u OTHER -u {TOKEN} true",
        f"sudo env -u {TOKEN} true",
        f"while true; do unset {TOKEN}; done",
        f"bash <<EOF\nunset {TOKEN}\nEOF",
        f"echo $(unset {TOKEN})",
    ],
)
def test_denies_token_unsets(command: str) -> None:
    assert guard.inspect(command)[0] == guard.UNSET_REASON


@pytest.mark.parametrize(
    "command",
    [
        "unset OTHER",
        "unset",
        f"echo 'unset {TOKEN}'",
        f'git commit -m "docs: never unset {TOKEN}"',
        f"printf '%s' 'env --unset={TOKEN}'",
        f"env --unset=OTHER echo {TOKEN}",
        f"echo {TOKEN}",
        "env -u OTHER true",
        "env --unset",
        f"ssh -o BatchMode=yes host 'echo {TOKEN}'",
        f"cat <<EOF\nunset {TOKEN}\nEOF",
        f"cat <<'EOF'\nenv -u {TOKEN} op whoami\nEOF",
        f"op run --env-file x -- printenv {TOKEN}",
    ],
)
def test_allows_other_unsets_and_quoted_mentions(command: str) -> None:
    assert guard.inspect(command)[0] is None


def wrapped(script: str) -> list[str]:
    """The script alone, and run by sh -c, bash -c, eval and ssh."""
    quoted = shlex.quote(script)
    return [
        script,
        f"sh -c {quoted}",
        f"bash -c {quoted}",
        f"eval {quoted}",
        f"ssh host {quoted}",
    ]


MORE_UNSETS = [
    # env: option clusters, option arguments and -S
    f"env -iu{TOKEN} op whoami",
    f"env -i -u {TOKEN} op whoami",
    f"env -C /work -u {TOKEN} op whoami",
    f"env -C/work -u {TOKEN} op whoami",
    f"env -vC /work -u{TOKEN} op whoami",
    f"env --chdir=/work -u {TOKEN} op whoami",
    f"env --chdir /work --unset {TOKEN} op whoami",
    f"env --argv0 x --unset={TOKEN} op whoami",
    f"env --uns {TOKEN} op whoami",
    f"env --un={TOKEN} op whoami",
    f"env -S '-u {TOKEN} op whoami'",
    f"env -S'-u {TOKEN}' op whoami",
    f"env --split-string='-C /work -u {TOKEN}' op whoami",
    f"env --split-string 'A=1 env -u {TOKEN} op whoami'",
    f'env -S "-u {TOKEN} \'op whoami"',
    f"env -u OTHER -S '-u {TOKEN}' op whoami",
    f"env -u ${TOKEN} op whoami",
    f"env -u '${{{TOKEN}}}' op whoami",
    # env drops the whole environment
    "env -i op whoami",
    "env -i",
    "env -0i op whoami",
    "env -vi op whoami",
    "env --ignore-environment op whoami",
    "env --ignore-e op whoami",
    "env --ignore-environment",
    "env - op whoami",
    "env -u OTHER -i op whoami",
    "env -C /work -i op whoami",
    "env -i gh pr view 1",
    "sudo env -i op whoami",
    "A=1 command env -i true",
    "env -S '-i op whoami'",
    # un-export
    f"export -n {TOKEN}",
    f"export -n {TOKEN}=x",
    f"export -fn {TOKEN}",
    f"export -nf {TOKEN}",
    f"export -n OTHER {TOKEN}",
    f"export -n -- {TOKEN}",
    f"export -n ${TOKEN}",
    f"declare +x {TOKEN}",
    f"declare -g +x {TOKEN}",
    f"declare +ix {TOKEN}",
    f"typeset +x {TOKEN}",
    f"local +x {TOKEN}",
    f"builtin export -n {TOKEN}",
    # an assignment that empties it
    f"{TOKEN}= op whoami",
    f"{TOKEN}='' op whoami",
    f'{TOKEN}="" op whoami',
    f"{TOKEN}=",
    f"{TOKEN}=''",
    f'{TOKEN}=""',
    f"A=1 {TOKEN}= op whoami",
    f"env {TOKEN}= op whoami",
    f"env -u OTHER {TOKEN}='' op whoami",
    f"sudo -E {TOKEN}= op whoami",
    f"export {TOKEN}=",
    f"export {TOKEN}=''",
    f'export A=1 {TOKEN}=""',
    f"declare -x {TOKEN}=",
    f"declare {TOKEN}=",
    f"typeset -x {TOKEN}=",
    f"local {TOKEN}=",
    f"export -- {TOKEN}=",
    # the unset builtin's other spellings of the name
    f"unset ${TOKEN}",
    f"unset ${{{TOKEN}}}",
    f'unset "${TOKEN}"',
    f'unset "${{{TOKEN}}}"',
    f"unset -v A ${{{TOKEN}}}",
]


@pytest.mark.parametrize(
    "command",
    [wrap for unset in MORE_UNSETS for wrap in wrapped(unset)],
)
def test_denies_more_token_unsets(command: str) -> None:
    assert guard.inspect(command)[0] == guard.UNSET_REASON


@pytest.mark.parametrize(
    "command",
    [
        # env options that aren't unsets, and option arguments
        "env true",
        "env -v true",
        "env -0 true",
        "env --debug true",
        "env --null true",
        "env -C /work true",
        "env --chdir=/work true",
        "env -u OTHER -C /work true",
        "env -S 'A=1 true'",
        "env -S 'A=1 \"x' true",
        "env -S",
        f"env -C {TOKEN} true",
        f"env --chdir {TOKEN} true",
        f"env --argv0 {TOKEN} true",
        f"env -a {TOKEN} true",
        f"env -S 'echo {TOKEN}'",
        f"env -C -u {TOKEN} true",
        # not env's options once the command starts, or env rejects them
        f"env true -u {TOKEN}",
        "env true -i",
        "env -- -i true",
        "env --ig true",
        "env --i true",
        "env --bogus true",
        "env --=x true",
        "env -ux true",
        # export, declare, typeset and local that keep it
        "export -n OTHER",
        "export -n",
        "export OTHER=",
        "export -p",
        f"export {TOKEN}",
        f"export {TOKEN}=value",
        f"export A=1 -n {TOKEN}",
        f"export -f {TOKEN}",
        "declare +x OTHER",
        f"declare -x {TOKEN}",
        f"typeset -x {TOKEN}",
        f"local {TOKEN}",
        f"local -x {TOKEN}=value",
        f"declare +i {TOKEN}",
        f"{TOKEN}=value op whoami",
        f"{TOKEN}=$A op whoami",
        f"OTHER= {TOKEN}=1 true",
        "OTHER= true",
        "OTHER=''",
        # data and out-of-scope forms
        f"echo {TOKEN}=",
        f"echo 'export -n {TOKEN}'",
        f"git commit -m 'env -i and {TOKEN}= and export -n {TOKEN}'",
        f"printf '%s' '{TOKEN}='",
        f"cat <<EOF\nenv -i op whoami\nexport -n {TOKEN}\nEOF",
        f"echo ${TOKEN} ${{{TOKEN}}}",
        "unset $OTHER",
        f'n={TOKEN}; unset "$n"',
        f"unset {TOKEN}x",
        f"unset x{TOKEN}",
        f"unset ${TOKEN}x",
    ],
)
def test_allows_option_arguments_and_other_variables(command: str) -> None:
    assert guard.inspect(command)[0] is None


@pytest.mark.parametrize(
    ("word", "args", "expected", "left"),
    [
        ("-i", [], ("i", ""), []),
        ("-", ["x"], ("i", ""), ["x"]),
        ("-iuNAME", [], ("i", ""), []),
        ("-vi", [], ("i", ""), []),
        ("-0", ["x"], ("", ""), ["x"]),
        ("-v", [], ("", ""), []),
        ("-uNAME", ["x"], ("u", "NAME"), ["x"]),
        ("-u", ["NAME", "x"], ("u", "NAME"), ["x"]),
        ("-u", [], ("u", ""), []),
        ("-vuNAME", [], ("u", "NAME"), []),
        ("-vu", ["NAME"], ("u", "NAME"), []),
        ("-uiNAME", [], ("u", "iNAME"), []),
        ("-C/work", ["x"], ("C", "/work"), ["x"]),
        ("-C", ["/work", "x"], ("C", "/work"), ["x"]),
        ("-S", ["a b"], ("S", "a b"), []),
        ("-a", ["x"], ("a", "x"), []),
        ("-P", ["x"], ("P", "x"), []),
        ("--unset=NAME", ["x"], ("u", "NAME"), ["x"]),
        ("--unset", ["NAME", "x"], ("u", "NAME"), ["x"]),
        ("--unset", [], ("u", ""), []),
        ("--uns", ["NAME"], ("u", "NAME"), []),
        ("--u=NAME", [], ("u", "NAME"), []),
        ("--chdir", ["d"], ("C", "d"), []),
        ("--chdir=d", ["x"], ("C", "d"), ["x"]),
        ("--split-string=a b", ["x"], ("S", "a b"), ["x"]),
        ("--argv0", ["a"], ("a", "a"), []),
        ("--ignore-environment", ["x"], ("i", ""), ["x"]),
        ("--ignore-e", [], ("i", ""), []),
        ("--ignore", ["x"], ("", ""), ["x"]),
        ("--i", ["x"], ("", ""), ["x"]),
        ("--debug", ["x"], ("v", ""), ["x"]),
        ("--null", ["x"], ("0", ""), ["x"]),
        ("--block-signal=INT", ["x"], ("", "INT"), ["x"]),
        ("--bogus=1", ["x"], ("", "1"), ["x"]),
        ("--", ["x"], ("", ""), ["x"]),
        ("--=x", ["y"], ("", "x"), ["y"]),
    ],
)
def test_env_option(
    word: str, args: list[str], expected: tuple[str, str], left: list[str]
) -> None:
    rest = list(args)
    assert guard.env_option(word, rest) == expected
    assert rest == left


@pytest.mark.parametrize(
    ("options", "expected"),
    [
        (["-iu" + TOKEN], None),
        (["-i", "-u", "A", "x"], None),
        (["-C", "/work", "-u", TOKEN], None),
        (["--chdir=/work", "-u", TOKEN], None),
        (["-C", "/work", "-u", "A", "x"], ["x"]),
        (["-C", "-u", TOKEN], [TOKEN]),
        (["-S", f"-u {TOKEN} x"], None),
        (["-S", "-u A x", "y"], ["x", "y"]),
        (["-S", "-C", "-u", TOKEN], [TOKEN]),
        (["-S", "A=1 x", "-u", TOKEN], ["A=1", "x", "-u", TOKEN]),
        (["-S", "-i x"], None),
        (["-S", "-C /work -u A", "x"], ["x"]),
        (["-S", "-u 'a b", "x"], ["b", "x"]),
        (["-u", f"${TOKEN}"], None),
        (["-u", f"${{{TOKEN}}}"], None),
        (["-u", f"{TOKEN}x"], []),
        (["-"], None),
        ([], []),
        (["--"], []),
    ],
)
def test_env_rest_parses_options(
    options: list[str], expected: list[str] | None
) -> None:
    assert guard.env_rest(options) == expected


@pytest.mark.parametrize(
    ("word", "expected"),
    [
        (TOKEN, True),
        (f"${TOKEN}", True),
        (f"${{{TOKEN}}}", True),
        (f"{TOKEN}=", False),
        (f"x{TOKEN}", False),
        (f"${{{TOKEN}", False),
        (f"{{{TOKEN}}}", False),
        (f"$({TOKEN})", False),
        ("", False),
    ],
)
def test_names_token(word: str, expected: bool) -> None:
    assert guard.names_token(word) is expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("a b", ["a", "b"]),
        ("-u 'a b' c", ["-u", "a b", "c"]),
        ('a "b', ["a", '"b']),
        ("", []),
    ],
)
def test_split_string(text: str, expected: list[str]) -> None:
    assert guard.split_string(text) == expected


@pytest.mark.parametrize(
    ("program", "args", "expected"),
    [
        ("export", ["-n", TOKEN], True),
        ("export", ["-n", f"{TOKEN}=x"], True),
        ("export", ["-fn", TOKEN], True),
        ("export", ["-f", "-n", "--", TOKEN], True),
        ("export", ["-n", "A", TOKEN], True),
        ("export", ["+n", TOKEN], False),
        ("export", ["-x", TOKEN], False),
        ("export", [TOKEN, "-n"], False),
        ("export", ["-n", "A"], False),
        ("export", [f"{TOKEN}="], True),
        ("export", [f"{TOKEN}=x"], False),
        ("declare", ["+x", TOKEN], True),
        ("declare", ["+x", "A=1", TOKEN], True),
        ("declare", ["-x", TOKEN], False),
        ("declare", ["-n", TOKEN], False),
        ("typeset", ["-g", "+ix", TOKEN], True),
        ("local", ["+x", TOKEN], True),
        ("local", [f"{TOKEN}="], True),
        ("readonly", ["-n", TOKEN], False),
        ("echo", ["-n", TOKEN], False),
        ("echo", [f"{TOKEN}="], False),
        ("export", [], False),
        ("export", ["", TOKEN], False),
    ],
)
def test_clears_token(program: str, args: list[str], expected: bool) -> None:
    assert guard.clears_token(program, args) is expected


def test_the_first_denial_wins() -> None:
    assert guard.inspect(f"unset {TOKEN}; gh run list")[0] == guard.UNSET_REASON
    assert guard.inspect(f"gh run list; unset {TOKEN}")[0] == guard.POLL_REASON


def test_inspect_reports_the_programs_it_runs() -> None:
    assert guard.inspect("echo a | bash -c 'gh pr view; sleep 1'") == (
        None,
        {"echo", "bash", "gh", "sleep"},
    )
    assert guard.inspect("A=1 env -u OTHER") == (None, set())
    assert guard.inspect("echo hi; gh run list") == (guard.POLL_REASON, {"echo"})


def test_body_scanner_keeps_each_commands_heredoc() -> None:
    scanner = guard.BodyScanner("cat <<A\none\nA\necho\nsh <<B <<C\ntwo\nB\nthree\nC\n")
    assert scanner.run() == [["cat"], ["echo"], ["sh"]]
    assert scanner.bodies == {0: "one\nA\n", 2: "two\nB\nthree\nC\n"}
    assert scanner.pending == []


@pytest.mark.parametrize(
    ("words", "expected"),
    [
        (["while", "true"], True),
        (["{", "until", "x"], True),
        (["do", "for", "x"], True),
        (["echo", "while"], False),
        (["if", "!"], False),
        ([], False),
    ],
)
def test_opens_loop(words: list[str], *, expected: bool) -> None:
    assert guard.opens_loop(words) is expected


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        (["host", "a", "b"], "a b"),
        (["-p", "22", "host", "x"], "x"),
        (["-t", "host", "x"], "x"),
        (["host"], None),
        (["-p"], None),
        (["-o"], None),
        ([], None),
    ],
)
def test_remote_command(args: list[str], expected: str | None) -> None:
    assert guard.remote_command(args) == expected


@pytest.mark.parametrize(
    ("words", "body", "expected"),
    [
        (["bash", "-c", "x"], "y", "x"),
        (["bash"], "y", "y"),
        (["bash", "-s"], "y", "y"),
        (["bash", "f.sh"], "y", None),
        (["bash"], None, None),
        (["/bin/sh", "-c", "x"], None, "x"),
        (["eval", "a", "b"], "y", "a b"),
        (["ssh", "h", "x"], "y", "x"),
        (["ssh", "h"], "y", "y"),
        (["cat"], "y", None),
    ],
)
def test_inner_script(words: list[str], body: str | None, expected: str | None) -> None:
    assert guard.inner_script(words, body) == expected


@pytest.mark.parametrize(
    ("options", "expected"),
    [
        (["-u", TOKEN, "x"], None),
        (["-u", "A", "x"], ["x"]),
        (["--unset", "A", "--unset", TOKEN], None),
        ([f"--unset={TOKEN}", "x"], None),
        ([f"-u{TOKEN}"], None),
        (["-i", "--", "x"], None),
        (["--", "-i"], ["-i"]),
        (["-u"], []),
        (["x", "-u", TOKEN], ["x", "-u", TOKEN]),
    ],
)
def test_env_rest(options: list[str], expected: list[str] | None) -> None:
    assert guard.env_rest(options) == expected


@pytest.mark.parametrize(
    ("words", "expected"),
    [
        (["A=1", "if", "sudo", "-E", "gh", "x"], ["gh", "x"]),
        (["env", "-u", "A", "env", "-u", TOKEN, "x"], None),
        (["nohup"], []),
        (["/usr/bin/env", "x"], ["x"]),
        (["gh", "-u", TOKEN], ["gh", "-u", TOKEN]),
    ],
)
def test_unwrapped(words: list[str], expected: list[str] | None) -> None:
    assert guard.unwrapped(words) == expected


@pytest.mark.parametrize("agy", [False, True])
@pytest.mark.parametrize(
    ("command", "reason"),
    [("gh run list", guard.POLL_REASON), (f"unset {TOKEN}", guard.UNSET_REASON)],
)
def test_hook_contract(
    command: str,
    reason: str,
    *,
    agy: bool,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    payload = (
        {"toolCall": {"args": {"CommandLine": command}}}
        if agy
        else {"tool_input": {"command": command}}
    )
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    assert alpha.main(["guard-gh-poll"], "tbhbdev alpha") == 0
    output = json.loads(capsys.readouterr().out)
    if agy:
        assert output == {"decision": "deny", "reason": reason}
    else:
        assert output == {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            }
        }


def test_reasons_tell_the_agent_to_subscribe_through_awrel() -> None:
    assert guard.POLL_REASON == (
        "Don't poll GitHub. Subscribe to the awrel event instead, for example "
        "check_run conclusion events for the repository through awrel, and act "
        "when it arrives. Bulk and polling GitHub reads are blocked; ask the "
        "operator for a capped exception."
    )
    assert guard.TOKEN == TOKEN
    unset = f"Don't unset {TOKEN}. Leave the environment as the harness gives it."
    assert unset == guard.UNSET_REASON


@pytest.mark.parametrize(
    "payload",
    ["invalid", "[]", "{}", '{"tool_input":{"command":"true"}}', ""],
)
def test_hook_allows_unreadable_or_allowed_input(
    payload: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys, "stdin", io.StringIO(payload))
    assert guard.main() == 0
    assert capsys.readouterr() == ("", "")


def test_hook_crashes_fail_open(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def crash(_text: str) -> None:
        raise RuntimeError

    monkeypatch.setattr(guard, "read_hook_stdin", lambda: {"tool_input": {}})
    monkeypatch.setattr(guard, "inspect", crash)
    assert guard.main() == 0
    assert capsys.readouterr() == ("", "")


def test_deep_nesting_fails_open(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    command = "eval " * 5000 + "gh run list"
    payload = json.dumps({"tool_input": {"command": command}})
    monkeypatch.setattr(sys, "stdin", io.StringIO(payload))
    assert guard.main() == 0
    assert capsys.readouterr() == ("", "")


def test_allowance_lets_each_granted_repository_list_once(grant_file: Path) -> None:
    count = grant_file.parent / "allowance.count"
    for repo in REPOS:
        assert guard.inspect(listing(repo))[0] is None
        assert guard.inspect(listing(repo))[0] == guard.POLL_REASON
    assert json.loads(count.read_text()) == dict.fromkeys(REPOS, 1)


def test_allowance_passes_through_a_wrapper(grant_file: Path) -> None:
    assert guard.inspect(f"bash -c '{listing(REPOS[0])}'")[0] is None
    count = json.loads((grant_file.parent / "allowance.count").read_text())
    assert count == {REPOS[0]: 1}


@pytest.mark.parametrize(
    "command",
    [
        f"gh run list -R example/three --limit 100 --json {FIELDS}",
        f"gh run list -R one --limit 100 --json {FIELDS}",
        f"gh run list -R example/one --limit 99 --json {FIELDS}",
        f"gh run list -R example/one --limit 100 --json {FIELDS},url",
        f"gh run list --limit 100 -R example/one --json {FIELDS}",
        f"gh run list -R example/one --limit 100 --json {FIELDS} --paginate",
        f"gh run list -R example/one --limit 100 --json {FIELDS} --branch x",
        "gh run list -R example/one --limit 100",
        "gh run list",
        listing("example/one").replace("gh run", "gh --hostname=example.com run"),
        f"gh run watch -R example/one --limit 100 --json {FIELDS}",
    ],
)
def test_allowance_refuses_any_other_shape(command: str, grant_file: Path) -> None:
    assert guard.inspect(command)[0] == guard.POLL_REASON
    assert not (grant_file.parent / "allowance.count").exists()


def test_a_loop_around_a_granted_listing_is_denied() -> None:
    command = f"while true; do {listing(REPOS[0])}; done"
    assert guard.inspect(command)[0] == guard.POLL_REASON


@pytest.mark.parametrize(("days", "allowed"), [(-10000, False), (-1, False), (0, True)])
def test_allowance_ends_after_its_expiry_date(
    grant_file: Path, days: int, *, allowed: bool
) -> None:
    # The date is taken at run time: a collection-time date goes stale if the
    # run crosses local midnight.
    expires = today() + timedelta(days=days)
    grant_file.write_text(json.dumps({"repos": REPOS, "expires": expires.isoformat()}))
    expected = None if allowed else guard.POLL_REASON
    assert guard.inspect(listing(REPOS[0]))[0] == expected


@pytest.mark.parametrize(
    "text",
    [
        "",
        "not json",
        "[]",
        "{}",
        '{"repos": ["example/one"]}',
        '{"expires": "2999-01-01"}',
        '{"repos": null, "expires": "2999-01-01"}',
        '{"repos": ["example/one"], "expires": "soon"}',
        '{"repos": ["example/one"], "expires": 20991231}',
    ],
)
def test_allowance_refuses_a_malformed_grant(text: str, grant_file: Path) -> None:
    grant_file.write_text(text)
    assert guard.inspect(listing(REPOS[0]))[0] == guard.POLL_REASON


def test_allowance_refuses_without_a_grant(grant_file: Path) -> None:
    grant_file.unlink()
    assert guard.inspect(listing(REPOS[0]))[0] == guard.POLL_REASON


@pytest.mark.parametrize("text", ["many\n", "", "[]", "3"])
def test_allowance_refuses_an_unreadable_count(text: str, grant_file: Path) -> None:
    count = grant_file.parent / "allowance.count"
    count.write_text(text)
    assert guard.inspect(listing(REPOS[0]))[0] == guard.POLL_REASON
    assert count.read_text() == text


def test_allowance_refuses_when_the_count_cannot_be_written(
    grant_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    blocker = grant_file.parent / "blocker"
    blocker.write_text("a file where the directory should be")
    monkeypatch.setattr(guard, "ALLOWANCE_COUNT", blocker / "allowance.count")
    assert guard.inspect(listing(REPOS[0]))[0] == guard.POLL_REASON


def test_run_view_of_one_run_still_passes_without_spending(grant_file: Path) -> None:
    command = "gh run view 42 -R example/one --json databaseId,jobs"
    assert guard.inspect(command)[0] is None
    assert not (grant_file.parent / "allowance.count").exists()


@pytest.mark.parametrize("name", ["gh-poll-allowance.json", "gh-poll-allowance.count"])
@pytest.mark.parametrize(
    "template",
    [
        "echo x > ~/.local/state/tbhb/{}",
        "rm {}",
        "cp a ~/.local/state/tbhb/{}",
        "sed -i s/a/b/ {}",
        "bash -c 'echo x | tee {}'",
    ],
)
def test_shell_commands_naming_the_allowance_files_are_denied(
    name: str, template: str
) -> None:
    assert guard.inspect(template.format(name))[0] == guard.PROTECT_REASON


def test_main_denies_a_write_to_an_allowance_file(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    for key in ("file_path", "notebook_path"):
        payload = {
            "tool_input": {key: "/home/x/.local/state/tbhb/gh-poll-allowance.json"}
        }
        monkeypatch.setattr(guard, "read_hook_stdin", lambda p=payload: p)
        assert guard.main() == 0
        assert guard.PROTECT_REASON in capsys.readouterr().out


@pytest.mark.parametrize(
    "payload",
    [
        {"tool_input": {"file_path": "/home/x/notes.md"}},
        {"tool_input": {"file_path": 3}},
        {"tool_input": "text"},
        {},
    ],
)
def test_main_allows_other_edits(
    payload: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(guard, "read_hook_stdin", lambda: payload)
    assert guard.main() == 0
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize(
    "template",
    [
        "echo x > ~/.local/state/tbhb/GH-POLL-ALLOWANCE.JSON",
        "rm Gh-Poll-Allowance.Count",
    ],
)
def test_shell_commands_naming_the_files_in_another_case_are_denied(
    template: str,
) -> None:
    assert guard.inspect(template)[0] == guard.PROTECT_REASON


@pytest.mark.parametrize("name", ["GH-POLL-ALLOWANCE.JSON", "Gh-Poll-Allowance.Count"])
def test_main_denies_an_edit_to_the_files_in_another_case(
    name: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    payload = {"tool_input": {"file_path": f"/home/x/.local/state/tbhb/{name}"}}
    monkeypatch.setattr(guard, "read_hook_stdin", lambda: payload)
    assert guard.main() == 0
    assert guard.PROTECT_REASON in capsys.readouterr().out


def test_main_denies_an_edit_through_a_link(
    grant_file: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    symlink = grant_file.parent / "notes.txt"
    symlink.symlink_to(grant_file)
    second_link = grant_file.parent / "other.txt"
    os.link(grant_file, second_link)
    for path in (symlink, second_link):
        payload = {"tool_input": {"file_path": str(path)}}
        monkeypatch.setattr(guard, "read_hook_stdin", lambda p=payload: p)
        assert guard.main() == 0
        assert guard.PROTECT_REASON in capsys.readouterr().out


def test_protects_ignores_an_empty_path_and_a_missing_file() -> None:
    assert not guard.protects("")
    assert not guard.protects("/nonexistent/notes.md")


@pytest.mark.parametrize(
    "command",
    [
        f"printf 'a\\nb\\n' | xargs -n1 sh -c '{listing('example/one')}'",
        f"xargs -n 1 {listing('example/one')}",
        f"parallel {listing('example/one')}",
        f"seq 2 | /usr/bin/xargs -I{{}} {listing('example/one')}",
    ],
)
def test_xargs_and_parallel_cannot_repeat_a_listing(
    command: str, grant_file: Path
) -> None:
    assert guard.inspect(command)[0] == guard.REPEAT_REASON
    assert not (grant_file.parent / "allowance.count").exists()


@pytest.mark.parametrize(
    "command",
    [
        'xargs -n1 sh -c \'gh r""un list -R example/one --limit 100 --json '
        + FIELDS
        + "'",
        "env -S 'xargs -n1 sh -c \"" + listing("example/one") + "\"'",
        'env xargs -n1 sh -c \'gh r""un list -R example/one --limit 100 --json '
        + FIELDS
        + "'",
        'parallel sh -c \'gh r""un list -R example/one --limit 100 --json '
        + FIELDS
        + "'",
    ],
)
def test_a_repeater_cannot_spend_the_allowance_through_wrapping_or_quoting(
    command: str, grant_file: Path
) -> None:
    assert guard.inspect(command)[0] is not None
    assert not (grant_file.parent / "allowance.count").exists()


def test_a_grant_of_deeply_nested_json_denies_a_listing(grant_file: Path) -> None:
    grant_file.write_text("[" * 100_000)
    assert guard.inspect(listing(REPOS[0]))[0] == guard.POLL_REASON


def test_a_count_of_deeply_nested_json_denies_a_listing(grant_file: Path) -> None:
    (grant_file.parent / "allowance.count").write_text("[" * 100_000)
    assert guard.inspect(listing(REPOS[0]))[0] == guard.POLL_REASON


QUOTED_LIST = "'gh r\"\"un list -R example/three --json databaseId'"


@pytest.mark.parametrize(
    "command",
    [
        "printf 'list -R example/one --limit 100 --json "
        + FIELDS
        + "\\n' | xargs -L1 gh run",
        "xargs -L1 -a args gh run",
        "parallel gh run :::: args",
    ],
)
def test_a_repeater_cannot_take_the_listing_words_from_input(
    command: str, grant_file: Path
) -> None:
    assert guard.inspect(command)[0] == guard.REPEAT_REASON
    assert not (grant_file.parent / "allowance.count").exists()


def test_xargs_may_run_other_gh_commands() -> None:
    assert guard.inspect("echo 1 2 | xargs -n1 gh pr view")[0] is None


def test_parallel_may_run_other_commands() -> None:
    assert guard.inspect("parallel -j2 echo ::: a b")[0] is None


@pytest.mark.parametrize(
    "prefix", ["> result ", ">result ", "2>/dev/null ", "2> err ", "< in ", ">> log "]
)
def test_a_leading_redirect_still_spends_the_allowance(
    prefix: str, grant_file: Path
) -> None:
    assert guard.inspect(prefix + listing(REPOS[0]))[0] is None
    count = json.loads((grant_file.parent / "allowance.count").read_text())
    assert count == {REPOS[0]: 1}
    assert guard.inspect(prefix + listing(REPOS[0]))[0] == guard.POLL_REASON


@pytest.mark.parametrize(
    "text",
    [
        '{"repos": {"example/one": 1}, "expires": "2999-01-01"}',
        '{"repos": ["example/one", 3], "expires": "2999-01-01"}',
        '{"repos": "example/one", "expires": "2999-01-01"}',
        '{"repos": ["", "example/one/"], "expires": "2999-01-01"}',
        '{"repos": ["."], "expires": "2999-01-01"}',
        '{"repos": ["../.."], "expires": "2999-01-01"}',
        '{"repos": ["./.", "example/.."], "expires": "2999-01-01"}',
        '{"repos": ["example\u00e4/one"], "expires": "2999-01-01"}',
        '{"repos": ["example/one", "x"], "expires": "2999-01-01"}',
    ],
)
def test_allowance_refuses_a_grant_with_a_malformed_repository_list(
    text: str, grant_file: Path
) -> None:
    grant_file.write_text(text)
    assert guard.inspect(listing("example/one"))[0] == guard.POLL_REASON
    assert not (grant_file.parent / "allowance.count").exists()


def test_allowance_counts_case_variants_of_a_repository_once(
    grant_file: Path,
) -> None:
    assert guard.inspect(listing("example/one"))[0] is None
    for variant in ("Example/One", "EXAMPLE/ONE", "example/One"):
        assert guard.inspect(listing(variant))[0] == guard.POLL_REASON
    count = json.loads((grant_file.parent / "allowance.count").read_text())
    assert count == {"example/one": 1}


def test_allowance_matches_a_grant_in_another_case(grant_file: Path) -> None:
    grant_file.write_text('{"repos": ["Example/One"], "expires": "2999-01-01"}')
    assert guard.inspect(listing("example/one"))[0] is None


def test_xargs_cannot_repeat_a_listing_split_by_a_line_continuation(
    grant_file: Path,
) -> None:
    script = listing("example/one").replace("run list", "run \\\nlist")
    command = f"printf 'a\\nb\\n' | xargs -n1 sh -c '{script}'"
    assert guard.inspect(command)[0] == guard.REPEAT_REASON
    assert not (grant_file.parent / "allowance.count").exists()


def test_a_link_to_the_count_file_is_protected_without_a_grant(
    grant_file: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    count = grant_file.parent / "allowance.count"
    count.write_text("{}\n")
    link = grant_file.parent / "tally.txt"
    os.link(count, link)
    grant_file.unlink()
    payload = {"tool_input": {"file_path": str(link)}}
    monkeypatch.setattr(guard, "read_hook_stdin", lambda: payload)
    assert guard.main() == 0
    assert guard.PROTECT_REASON in capsys.readouterr().out


@pytest.mark.parametrize(
    "name", ["gh-poll-allowance.count.lock", "gh-poll-allowance.json.bak"]
)
def test_a_lock_or_backup_copy_of_a_protected_file_is_protected(name: str) -> None:
    assert guard.protects(f"/home/x/.local/state/tbhb/{name}")


def test_an_existing_unrelated_file_is_not_protected(grant_file: Path) -> None:
    other = grant_file.parent / "notes.txt"
    other.write_text("x")
    assert not guard.protects(str(other))


def test_granted_repos_rejects_a_dotted_path_among_valid_ones() -> None:
    with pytest.raises(ValueError, match=r"^malformed grant$"):
        guard.granted_repos({"repos": ["example/one", "../x"]})


def test_allowance_refuses_a_grant_that_lists_a_dotted_path_with_a_valid_one(
    grant_file: Path,
) -> None:
    grant_file.write_text(
        json.dumps({"repos": ["example/one", "../x"], "expires": "2999-01-01"})
    )
    assert guard.inspect(listing("example/one"))[0] == guard.POLL_REASON


def test_allowance_creates_missing_parents_and_a_named_lock(
    grant_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    count = grant_file.parent / "a" / "b" / "allowance.count"
    monkeypatch.setattr(guard, "ALLOWANCE_COUNT", count)
    assert guard.inspect(listing("example/one"))[0] is None
    assert count.exists()
    assert "allowance.count.lock" in {p.name for p in count.parent.iterdir()}


def test_defaults_treat_a_call_as_not_repeated(grant_file: Path) -> None:
    repos = ["example/a", "example/b", "example/c", "example/d"]
    grant_file.write_text(json.dumps({"repos": repos, "expires": "2999-01-01"}))
    words = ["-R", repos[0], *guard.ALLOWANCE_TAIL]
    assert guard.github_poll(["run", "list", *words]) is False
    assert guard.scan_commands(listing(repos[1]))[0] is None
    assert guard.denial(shlex.split(listing(repos[2])), None)[0] is None
    assert guard.checked(shlex.split(listing(repos[3])), None)[0] is None


def test_unwrap_of_no_words_reports_no_repeater() -> None:
    assert guard.unwrap([]) == ([], False)


def test_repeated_scan_collects_the_programs_of_every_suffix() -> None:
    assert guard.repeated_scan(["echo", "ls"]) == (None, {"echo", "ls"})


def test_edited_file_names_nothing_for_other_calls() -> None:
    assert guard.edited_file({}) == ""
    assert guard.edited_file({"tool_input": {"file_path": 5}}) == ""
    assert guard.edited_file({"tool_input": {"file_path": "/a/b"}}) == "/a/b"


def test_protects_matches_the_link_name_and_the_target_name_in_any_case(
    tmp_path: Path,
) -> None:
    plain = tmp_path / "plain.txt"
    plain.write_text("x")
    by_link = tmp_path / "GH-POLL-ALLOWANCE.json"
    by_link.symlink_to(plain)
    assert guard.protects(str(by_link))
    target = tmp_path / "GH-POLL-ALLOWANCE.COUNT"
    target.write_text("x")
    link = tmp_path / "innocent"
    link.symlink_to(target)
    assert guard.protects(str(link))


def test_a_count_key_in_another_case_still_counts_as_spent(grant_file: Path) -> None:
    (grant_file.parent / "allowance.count").write_text('{"Example/One": 1}')
    assert guard.inspect(listing("example/one"))[0] == guard.POLL_REASON


@pytest.mark.parametrize(
    "command",
    [
        "printf 'list\\nlist\\n' | env -S 'xargs -L1 gh run'",
        "env -S 'parallel gh run'",
        "printf 'a\\nb\\n' | xargs -n 1 sh -c " + QUOTED_LIST,
        "printf 'a\\nb\\n' | xargs -L 1 sh -c 'gh r\"\"un list -R example/three'",
    ],
)
def test_a_repeater_hidden_in_an_option_value_or_env_string_is_denied(
    command: str, grant_file: Path
) -> None:
    assert guard.inspect(command)[0] is not None
    assert not (grant_file.parent / "allowance.count").exists()


def test_flattened_splits_quoted_words_to_a_depth() -> None:
    assert guard.flattened(["env", "-S", "xargs gh r''un"]) == [
        "env", "-S", "xargs", "gh", "run",
    ]  # fmt: skip
    assert guard.flattened(["a b"], depth=0) == ["a b"]
    assert guard.flattened(["'a b'"], depth=0) == ["'a b'"]
    assert guard.flattened(['x.c""ount']) == ["x.count"]
    assert guard.flattened(["plain"]) == ["plain"]


@pytest.mark.parametrize(
    "command",
    [
        'rm ~/.local/state/tbhb/gh-poll-allowance.c""ount',
        "rm ~/.local/state/tbhb/gh-poll-allowance.'count'",
        "rm -rf ~/.local/state/tbhb",
        "rm -rf ~/.local/state/tbhb/",
        "rm -rf $HOME/.local/state/tbhb",
        "rm -rf ${HOME}/.local/state",
        "cp -a ~/.local/state/tbhb /tmp/snap",
        "mv /tmp/snap ~/.local/state/tbhb",
        "env rm -rf ~/.local",
    ],
)
def test_protected_names_are_read_after_unquoting_and_the_state_directory_is_kept(
    command: str,
) -> None:
    assert guard.inspect(command)[0] == guard.PROTECT_REASON


@pytest.mark.parametrize(
    "command",
    [
        "rm -rf /tmp/snap",
        "rm -rf ~/.local/state/other",
        "ls ~/.local/state/tbhb",
        "cp -a /tmp/a /tmp/b",
        "rm -rf relative/dir",
    ],
)
def test_other_paths_and_programs_pass_the_state_directory_check(command: str) -> None:
    assert guard.inspect(command)[0] is None


@pytest.mark.parametrize(
    ("word", "expected"),
    [
        ("~/.local/state/tbhb", True),
        ("~/.local/state/tbhb/", True),
        ("$HOME/.local/state", True),
        ("${HOME}/.local", True),
        ("~/.local/state/tbhb/sub", False),
        ("~/.local/state/other", False),
        ("tbhb", False),
        ("-rf", False),
    ],
)
def test_names_state_dir(word: str, *, expected: bool) -> None:
    assert guard.names_state_dir(word) is expected


def test_a_grant_matches_only_the_listed_repository(grant_file: Path) -> None:
    other = "example/unlisted"
    assert guard.inspect(listing(other))[0] == guard.POLL_REASON
    assert not (grant_file.parent / "allowance.count").exists()
    assert guard.allowed_call(
        ["-R", REPOS[0].upper(), *guard.ALLOWANCE_TAIL], {REPOS[0].lower()}
    )
    assert not guard.allowed_call(["-R", REPOS[0], *guard.ALLOWANCE_TAIL], {other})
    assert not guard.allowed_call(
        ["-R", REPOS[0].upper(), *guard.ALLOWANCE_TAIL], {REPOS[0].upper()}
    )


@pytest.mark.parametrize(
    "command",
    [
        "xargs gh api repos/o/r/actions/runs",
        "parallel gh api repos/o/r/actions/runs",
        "parallel gh pr checks 1 --watch",
        "xargs gh pr checks 1 --watch",
        "xargs env -u " + TOKEN + " true",
    ],
)
def test_a_repeater_still_scans_what_it_runs(command: str) -> None:
    assert guard.inspect(command)[0] is not None


def test_flattened_splits_nested_words_four_levels_deep() -> None:
    levels = [shlex.join(["a", "t1"])]
    for n in range(2, 7):
        levels.append(shlex.join([levels[-1], f"t{n}"]))
    # Splitting the sixth level peels it and the three below it; the second stays whole.
    assert guard.flattened([levels[5]]) == [levels[1], "t3", "t4", "t5", "t6"]


def test_checked_passes_the_repeated_flag_to_the_listing_check(
    grant_file: Path,
) -> None:
    command = listing(REPOS[0]).split()
    assert guard.checked(command, None, repeated=True)[0] == guard.POLL_REASON
    assert not (grant_file.parent / "allowance.count").exists()
    assert guard.checked(command, None)[0] is None
    assert (grant_file.parent / "allowance.count").exists()


@pytest.mark.parametrize("program", ["/bin/rm", "/usr/bin/env rm"])
def test_a_program_path_does_not_hide_a_state_directory_removal(program: str) -> None:
    assert guard.protected_words([*program.split(), "-rf", "~/.local/state/tbhb"])

# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns
"""Tests for tbhbdev alpha guard-gh-poll, which shares guard-gh-pr's scanner."""

import io
import itertools
import json
import shlex
import sys
from typing import TYPE_CHECKING

import pytest

from tbhbdev import alpha
from tbhbdev.alpha import guard_gh_poll as guard

if TYPE_CHECKING:
    from collections.abc import Callable

SHA = "abcdef0123456789abcdef0123456789abcdef01"
# The 1Password service account token's variable, split for gitleaks.
TOKEN = "OP_SERVICE_" + "ACCOUNT_TOKEN"


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
    hook_stdin: Callable[[str, bool], None],
    capsys: pytest.CaptureFixture[str],
) -> None:
    hook_stdin(command, agy)
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

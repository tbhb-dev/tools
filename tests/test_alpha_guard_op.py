# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns
import json
from pathlib import Path
from typing import TYPE_CHECKING, Never

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping

import pytest

from tbhbdev import alpha, userconfig
from tbhbdev.alpha import guard_op as guard

ACCOUNT = "example.1password.com"


def configured(table: str, key: str) -> str:
    assert (table, key) == ("op", "account")
    return ACCOUNT


@pytest.fixture(autouse=True)
def account(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(userconfig, "string", configured)


SHAPES = [
    "op item get x",
    "/opt/homebrew/bin/op item get x",
    "'op' item get x",
    "FOO=1 op item get x",
    "env op item get x",
    "command op item get x",
    "exec op item get x",
    "sudo -u agent op item get x",
    "sudo -g staff op item get x",
    "timeout 30 op item get x",
    "timeout -s TERM 30 op item get x",
    "nice -n 5 op item get x",
    "mise x -- op item get x",
    "mise exec -- op item get x",
    "xargs op item get x",
    "x | op item get x",
    "a && op item get x",
    "a; op item get x",
    "echo $(op item get x{flag})",
    'echo "$(op item get x{flag})"',
    "echo `op item get x{flag}`",
    "sh -c 'op item get x{flag}'",
    'bash -lc "op item get x{flag}"',
    "eval op item get x",
    "ssh -o X=y host 'op item get x{flag}'",
    "bash <<'EOF'\nop item get x{flag}\nEOF\n",
    "ssh host <<'EOF'\nop item get x{flag}\nEOF\n",
    "OP_ACCOUNT=x op item get x",
    "op \\\nitem get x",
    "op item get x \\\n{flag}",
]


@pytest.mark.parametrize("shape", SHAPES)
@pytest.mark.parametrize("flag", ["", f" --account {ACCOUNT}", f" --account={ACCOUNT}"])
def test_shell(shape: str, flag: str) -> None:
    text = shape.format(flag=flag) if "{flag}" in shape else shape + flag
    check = guard.Check()
    assert bool(check.offenders(guard.shell_calls(text))) == (not flag)


@pytest.mark.parametrize(
    "text",
    [
        "opam",
        "echo op",
        'echo "op connect token create printed no token"',
        "git commit -m 'op item get'",
        "grep op file",
        "\n# comment\n",
        "mise x op item get x",
        "env",
        "bash script.sh",
    ],
)
def test_prose(text: str) -> None:
    assert not guard.Check().offenders(guard.shell_calls(text))


@pytest.mark.parametrize(
    "flags",
    [
        "--account wrong",
        f"--account {ACCOUNT} --account wrong",
        "--account",
        "--account=",
    ],
)
def test_wrong(flags: str) -> None:
    assert guard.Check().offenders(guard.shell_calls("op read x " + flags))


def book(source: str) -> str:
    return json.dumps({"cells": [{"id": "one", "cell_type": "code", "source": source}]})


FILES = [
    ("x.sh", "op read x\n"),
    ("x.bash.j2", "op read x\n"),
    ("x", "#!/usr/bin/env zsh\nop read x\n"),
    ("x.md", "```bash\nop read x\n```\n"),
    ("x.md", "~~~console\n$ op read x\nignored op read y\n# op read z\n~~~\n"),
    ("x.py", '["/opt/homebrew/bin/op", "read", ref]\n'),
    ("x.py", 'subprocess.run("op read x", shell=True)\n'),
    ("x.py", 'os.system("op read x")\n'),
    ("x.py", 'os.popen("op read x")\n'),
    ("x.yml", "jobs:\n  build:\n    steps:\n      - run: op read x\n"),
    ("x.yaml", "- shell: op item get x\n"),
    ("x.yml", "argv: [op, read, x]\n"),
    ("x.ipynb", book("!op read x\n")),
    ("x.ipynb", book("%%bash\nop read x\n")),
    ("x.ipynb", book('["op", "read", ref]\n')),
]


@pytest.mark.parametrize(("name", "content"), FILES)
def test_write_new_old_swap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    name: str,
    content: str,
) -> None:
    path = tmp_path / name
    payload = {
        "tool_name": "Write",
        "tool_input": {"file_path": str(path), "content": content},
    }
    monkeypatch.setattr(guard, "read_hook_stdin", lambda: payload)
    assert guard.main() == 0
    assert "permissionDecision" in capsys.readouterr().out
    path.write_text(content)
    payload["tool_input"]["content"] = content + "\n"
    assert guard.main() == 0
    assert not capsys.readouterr().out
    payload["tool_input"]["content"] = content.replace("read", "delete").replace(
        "get", "delete"
    )
    assert guard.main() == 0
    assert "permissionDecision" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("name", "content"),
    [
        ("x.md", "op read x\n```python\nop read x\n```\n"),
        ("x.md", "```console\nop read x\n```\n"),
        ("x.md", "```sh\nop read x --account example.1password.com\n```\n"),
        ("x.py", '["op", "read", ref, "--account", ACCOUNT]'),
        ("x.py", '("op", *args, "--account", account)'),
        ("x.py", '["op", "--account=anything"]'),
        ("x.py", 'log("op read x printed no token")'),
        ("x.py", 'other.run("op read x", shell=True)'),
        ("x.py", 'subprocess.run("op read x")'),
        ("x.py", "os.system(variable)"),
        ("x.py", '["echo", "op"]'),
        ("x.py", "[]\nx = 1\n%magic\n!ignored"),
        ("x.py", "invalid python :"),
        ("x.yaml", "- shell: op item get x --account example.1password.com"),
        ("x.yaml", "argv: [op, --account, anything]\nother: 3"),
        ("x.yaml", "["),
        ("x.txt", "op read x"),
        (
            "x.ipynb",
            json.dumps({"cells": [{"cell_type": "markdown", "source": "op read x"}]}),
        ),
        ("x.ipynb", book("%%sh\necho op\n")),
    ],
)
def test_allowed_file(name: str, content: str) -> None:
    assert not guard.Check().offenders(guard.file_calls(Path(name), content))


def invoke(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    payload: Mapping[str, object],
) -> str:
    payload = dict(payload)
    call = payload.get("toolCall")
    if isinstance(call, dict):
        payload["toolCall"] = {"name": "run_command", **call}
    else:
        payload.setdefault("tool_name", "Bash")
    monkeypatch.setattr(guard, "read_hook_stdin", lambda: payload)
    assert guard.main() == 0
    return capsys.readouterr().out


@pytest.mark.parametrize("flag", ["", " --account example.1password.com"])
def test_patch_and_agy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    flag: str,
) -> None:
    patch = f"*** Begin Patch\n*** Add File: new.sh\n+op read x{flag}\n*** End Patch"
    payload = {
        "cwd": str(tmp_path),
        "tool_name": "apply_patch",
        "tool_input": {"input": patch},
    }
    assert bool(invoke(monkeypatch, capsys, payload)) == (not flag)
    payload = {
        "toolCall": {
            "name": "write_to_file",
            "args": {
                "TargetFile": str(tmp_path / "x.sh"),
                "CodeContent": f"op read x{flag}",
            },
        }
    }
    assert bool(invoke(monkeypatch, capsys, payload)) == (not flag)


@pytest.mark.parametrize(
    "tool", ["Edit", "MultiEdit", "replace_file_content", "multi_replace_file_content"]
)
def test_edits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tool: str,
) -> None:
    path = tmp_path / "x.sh"
    path.write_text("echo x\n")
    edit = {"old_string": "echo", "new_string": "op"}
    inputs = {"file_path": str(path), **edit}
    if tool == "MultiEdit":
        inputs = {"file_path": str(path), "edits": [edit]}
    if tool in {"replace_file_content", "multi_replace_file_content"}:
        inputs = {
            "TargetFile": str(path),
            "ReplacementChunks": [
                {"TargetContent": "echo", "ReplacementContent": "op"}
            ],
        }
        payload = {"toolCall": {"name": tool, "args": json.dumps(inputs)}}
    else:
        payload = {"tool_name": tool, "tool_input": inputs}
    assert invoke(monkeypatch, capsys, payload)


@pytest.mark.parametrize("mode", ["replace", "insert", "delete"])
def test_notebook_edit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    mode: str,
) -> None:
    path = tmp_path / "x.ipynb"
    path.write_text(book("print(1)"))
    payload = {
        "tool_name": "NotebookEdit",
        "tool_input": {
            "notebook_path": str(path),
            "cell_id": "one",
            "edit_mode": mode,
            "new_source": "!op read x",
        },
    }
    assert bool(invoke(monkeypatch, capsys, payload)) == (mode != "delete")


def test_failures(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def crash(*_: object) -> Never:
        raise RuntimeError

    monkeypatch.setattr(guard, "read_hook_stdin", crash)
    assert guard.main() == 0
    assert not capsys.readouterr().out
    monkeypatch.setattr(
        guard,
        "read_hook_stdin",
        lambda: {"tool_name": "Bash", "tool_input": {"command": "op read x"}},
    )
    monkeypatch.setattr(userconfig, "string", crash)
    assert guard.main() == 0
    assert "Set [op] account" in capsys.readouterr().out
    monkeypatch.setattr(userconfig, "string", configured)

    def partial(_: str) -> Iterator[list[str]]:
        yield ["op", "read", "x", "--account", ACCOUNT]
        raise RuntimeError

    monkeypatch.setattr(guard, "shell_calls", partial)
    assert guard.main() == 0
    out = capsys.readouterr().out
    assert "add --account example.1password.com" in out
    assert "`op read x --account example.1password.com`" in out


def test_entry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(guard, "main", lambda: 7)
    assert alpha.main(["guard-op"], "p") == 7


@pytest.mark.parametrize(
    "text",
    [
        "cat <<EOF\nop read x\nEOF\n",
        "bash -c 'echo hello' <<EOF\nop read x\nEOF\n",
        "ssh host 'echo hello' <<EOF\nop read x\nEOF\n",
        "env <<EOF\nop read x\nEOF\n",
    ],
)
def test_data_heredocs(text: str) -> None:
    assert not list(guard.shell_calls(text))


def test_nested_python_attribute() -> None:
    assert not list(guard.python_calls('client.os.system("op read x")'))


@pytest.mark.parametrize("inputs", [None, {}])
def test_other_payloads(
    inputs: object, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert not invoke(monkeypatch, capsys, {"tool_input": inputs})


def test_patch_update_move_delete(tmp_path: Path) -> None:
    source = tmp_path / "x.sh"
    source.write_text("#!/bin/sh\necho first\necho second\n")
    patch = """*** Begin Patch
*** Update File: x.sh
@@
 #!/bin/sh
-echo first
+op read x
@@
-echo second
+echo last
*** Move to: y.sh
*** Delete File: unused.sh
*** End Patch
"""
    result = list(guard.patch_files(patch, tmp_path))
    assert result == [
        (tmp_path / "y.sh", source.read_text(), "#!/bin/sh\nop read x\necho last\n"),
        (tmp_path / "unused.sh", "", ""),
    ]
    assert source.read_text() == "#!/bin/sh\necho first\necho second\n"
    current = guard.Patch(source, "", "")
    current.line("ignored")
    assert current.after == ""


@pytest.mark.parametrize(
    "text",
    [
        "bash <<op\necho safe\nop\n",
        "bash <<-op\necho safe\n\top\n",
        "bash <<op\necho safe\n",
        "bash <<op\n",
        "bash <<one <<two\necho first\none\necho second\ntwo\n",
    ],
)
def test_heredoc_delimiters_are_data(text: str) -> None:
    assert not list(guard.shell_calls(text))


def test_denial_contracts(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    message = (
        "`op read x` is blocked. Use the configured account example.1password.com: "
        "add --account example.1password.com."
    )
    assert json.loads(
        invoke(monkeypatch, capsys, {"tool_input": {"command": "op read x"}})
    ) == {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": message,
        }
    }
    assert json.loads(
        invoke(
            monkeypatch, capsys, {"toolCall": {"args": {"CommandLine": "op read x"}}}
        )
    ) == {
        "decision": "deny",
        "reason": message,
    }
    assert not invoke(
        monkeypatch,
        capsys,
        {"tool_input": {"command": f"op read x --account {ACCOUNT}"}},
    )


def test_missing_account(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def missing(_table: str, _key: str) -> str:
        raise userconfig.ConfigError

    monkeypatch.setattr(userconfig, "string", missing)
    assert "Set [op] account in the user config." in invoke(
        monkeypatch, capsys, {"tool_input": {"command": "op read x"}}
    )
    assert not invoke(monkeypatch, capsys, {"tool_input": {"command": "echo op"}})


@pytest.mark.parametrize(
    "text",
    [
        "timeout --signal TERM 20 op read x",
        "nice op read x",
        "sudo --user agent --group staff op read x",
        "env FOO=1 command exec /usr/bin/op read x",
        "ssh -v -p 22 -i key -l user -J jump host op read x",
        "bash -c 'timeout 3 op read x'",
        "mise exec --cd . -- op read x",
    ],
)
def test_extended_prefixes(text: str) -> None:
    assert list(guard.shell_calls(text)) == [["op", "read", "x"]] or list(
        guard.shell_calls(text)
    ) == [["/usr/bin/op", "read", "x"]]


def test_edit_set_difference(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "script.sh"
    path.write_text("op read x\n")
    payload = {
        "tool_name": "Write",
        "tool_input": {"file_path": str(path), "content": "op read x\nop read x\n"},
    }
    assert not invoke(monkeypatch, capsys, payload)


def test_replace_all() -> None:
    assert (
        guard.replacements(
            "echo x; echo y",
            [{"old_string": "echo", "new_string": "op", "replace_all": True}],
        )
        == "op x; op y"
    )


def test_heredoc_pipeline() -> None:
    assert list(guard.shell_calls("bash <<EOF | cat\nop read x\nEOF\n")) == [
        ["op", "read", "x"]
    ]
    assert not list(guard.shell_calls("cat <<EOF | bash\nop read x\nEOF\n"))


@pytest.mark.parametrize("tool", ["write_file", "edit_file"])
@pytest.mark.parametrize("flag", ["", " --account example.1password.com"])
def test_agy_cli_edits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tool: str,
    flag: str,
) -> None:
    path = tmp_path / "cli.sh"
    path.write_text("echo x\n")
    args = (
        {"Path": str(path), "Content": f"op read x{flag}"}
        if tool == "write_file"
        else {
            "Path": str(path),
            "OldText": "echo x",
            "NewText": f"op read x{flag}",
            "ReplaceAll": True,
        }
    )
    assert bool(
        invoke(monkeypatch, capsys, {"toolCall": {"name": tool, "args": args}})
    ) == (not flag)


def test_patch_ordered_repeated_hunks(tmp_path: Path) -> None:
    path = tmp_path / "repeated.sh"
    before = "echo x\necho y\necho x\n"
    path.write_text(before)
    patch = """*** Begin Patch
*** Update File: repeated.sh
@@
 echo x
-echo y
+echo z
@@
-echo x
+op read x
*** End Patch
"""
    assert list(guard.patch_files(patch, tmp_path)) == [
        (path, before, "echo x\necho z\nop read x\n")
    ]


def test_notebook_magic_options() -> None:
    assert list(guard.notebook_calls(book("%%bash -e\nop read x"))) == [
        ["op", "read", "x"]
    ]


@pytest.mark.parametrize(
    "text",
    [
        "/usr/bin/timeout 30 op read x",
        "env -- mise x -- sudo -u agent op read x",
        "FOO=1 sudo -u agent timeout 30 op read x",
        '"o\\\np" read x',
    ],
)
def test_prefix_combinations(text: str) -> None:
    assert guard.Check().offenders(guard.shell_calls(text))


@pytest.mark.parametrize(
    "text",
    [
        '%magic\n["op", "read", ref]',
        '  %magic\n["op", "read", ref]',
        '!magic\n["op", "read", ref]',
        '  !magic\n["op", "read", ref]',
        'x = 1\n["op", "read", ref]',
    ],
)
def test_python_magic_filter(text: str) -> None:
    assert list(guard.python_calls(text)) == [["op", "read", "ref"]]


@pytest.mark.parametrize(
    "text",
    [
        'other.system("op read x")',
        'os.other("op read x")',
        'subprocess.run("op read x", other=True)',
        'subprocess.run("op read x", shell=False)',
    ],
)
def test_python_other_calls(text: str) -> None:
    assert not list(guard.python_calls(text))


@pytest.mark.parametrize("tag", ["sh", "bash", "shell", "zsh", "console"])
def test_markdown_blocks(tag: str) -> None:
    source = "op read x\nop read y" if tag != "console" else "$ op read x\n# op read y"
    text = f"```{tag}\n{source}\n```\nprose op read z\n```{tag}\n{source}\n```\n"
    assert (
        list(guard.markdown_calls(text))
        == [["op", "read", "x"], ["op", "read", "y"]] * 2
    )


def test_notebook_multiple_cells() -> None:
    cells = [
        {"cell_type": "markdown", "source": "op read z"},
        {"cell_type": "code", "source": ["%%sh\n", "op read x\n"]},
        {"cell_type": "code", "source": "  !op read y\n"},
        {"cell_type": "code"},
    ]
    assert list(guard.notebook_calls(json.dumps({"cells": cells}))) == [
        ["op", "read", "x"],
        ["op", "read", "y"],
    ]


def test_yaml_prose() -> None:
    assert not list(guard.yaml_calls({"message": "op read x"}))


def test_zsh_file() -> None:
    assert list(guard.file_calls(Path("x.zsh"), "op read x")) == [["op", "read", "x"]]


def test_patch_search_and_truncation(tmp_path: Path) -> None:
    path = tmp_path / "x: a.sh"
    before = "keep\necho x\necho x\n"
    path.write_text(before)
    patch = """*** Begin Patch
*** Update File: x: a.sh
@@
-echo x
+echo y
@@
-echo x
+op read x
*** Move to: y: a.sh
"""
    assert list(guard.patch_files(patch, tmp_path)) == [
        (tmp_path / "y: a.sh", before, "keep\necho y\nop read x\n")
    ]


@pytest.mark.parametrize("key", ["replace_all", "ReplaceAll"])
def test_multiple_replacements(key: str) -> None:
    edit: dict[str, object] = {"old_string": "x", "new_string": "y", key: True}
    assert guard.replacements("xxx", [edit]) == "yyy"
    edit[key] = False
    assert guard.replacements("xxx", [edit]) == "yxx"


@pytest.mark.parametrize("key", ["input", "patch", "command"])
def test_patch_payload_spellings(tmp_path: Path, key: str) -> None:
    patch = "*** Begin Patch\n*** Add File: x.sh\n+op read x\n*** End Patch"
    assert list(
        guard.writes(
            {
                "cwd": str(tmp_path),
                "tool_name": "apply_patch",
                "tool_input": {key: patch},
            }
        )
    ) == [(tmp_path / "x.sh", "", "op read x\n")]


@pytest.mark.parametrize("mode", ["replace", "insert", "delete"])
def test_notebook_cell_selection(mode: str) -> None:
    first = {"id": "first", "cell_type": "code", "source": "print(1)"}
    second = {"id": "second", "cell_type": "code", "source": "print(2)"}
    inputs: dict[str, object] = {
        "cell_id": "second",
        "edit_mode": mode,
        "new_source": "op read x",
        "cell_type": "markdown",
    }
    book_before = json.dumps({"cells": [first, second]})
    expected = [first, {**second, "source": "op read x", "cell_type": "markdown"}]
    if mode == "insert":
        expected = [first, {"cell_type": "markdown", "source": "op read x"}, second]
    elif mode == "delete":
        expected = [first]
    assert json.loads(guard.notebook_edit(book_before, inputs)) == {"cells": expected}
    assert json.loads(guard.notebook_edit(book_before, {"new_source": "updated"})) == {
        "cells": [first, second]
    }


def test_wrong_account_with_equals() -> None:
    assert not guard.valid_account(
        ["op", "--account=wrong=example.1password.com"], ACCOUNT
    )


def test_main_retains_first_patch_offender(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    patch = (
        "*** Begin Patch\n*** Add File: x.sh\n+op read a\n"
        "*** Add File: y.sh\n+echo safe\n*** End Patch"
    )
    out = invoke(
        monkeypatch,
        capsys,
        {
            "cwd": str(tmp_path),
            "tool_name": "apply_patch",
            "tool_input": {"input": patch},
        },
    )
    assert "op read a" in out
    patch = "*** Begin Patch\n*** Add File: x.sh\n+op read a\n+op read z\n*** End Patch"
    out = invoke(
        monkeypatch,
        capsys,
        {
            "cwd": str(tmp_path),
            "tool_name": "apply_patch",
            "tool_input": {"input": patch},
        },
    )
    assert "`op read a`" in out


@pytest.mark.parametrize(
    "text",
    [
        "sudo -u agent bash -c 'op read x'",
        "timeout 30 eval op read x",
        "mise x -- sh -c 'op read x'",
    ],
)
def test_wrapped_shells(text: str) -> None:
    assert list(guard.shell_calls(text)) == [["op", "read", "x"]]


@pytest.mark.parametrize(
    ("name", "content"),
    [
        ("x.py", '["op", "read", ref, "--account", ACCOUNT]'),
        ("x.yaml", "argv: [op, read, x, --account, anything]"),
    ],
)
def test_argv_needs_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    name: str,
    content: str,
) -> None:
    def missing(_table: str, _key: str) -> str:
        raise userconfig.ConfigError

    monkeypatch.setattr(userconfig, "string", missing)
    assert "Set [op] account" in invoke(
        monkeypatch,
        capsys,
        {
            "tool_name": "Write",
            "tool_input": {"file_path": str(tmp_path / name), "content": content},
        },
    )


def test_yaml_other_argv() -> None:
    assert not list(guard.yaml_calls({"argv": ["echo", "op"]}))


def test_empty_wrapper_before_call() -> None:
    assert list(guard.shell_calls("timeout 5; mise x --; op read x")) == [
        ["op", "read", "x"]
    ]


def test_unclosed_markdown_fence() -> None:
    assert list(guard.markdown_calls("```sh\nop read x\nop read y")) == [
        ["op", "read", "x"],
        ["op", "read", "y"],
    ]
    assert not list(guard.markdown_calls("```python\nop read x"))


def test_patch_without_final_newline(tmp_path: Path) -> None:
    path = tmp_path / "x.sh"
    path.write_text("echo x")
    patch = (
        "*** Begin Patch\n*** Update File: x.sh\n@@\n-echo x\n+op read x\n*** End Patch"
    )
    assert list(guard.patch_files(patch, tmp_path)) == [(path, "echo x", "op read x\n")]


def test_patch_uses_context_and_separated_hunks(tmp_path: Path) -> None:
    path = tmp_path / "x.sh"
    before = "echo same\n# choose\necho same\necho keep\necho last\n"
    path.write_text(before)
    patch = """*** Begin Patch
*** Update File: x.sh
@@
 # choose
-echo same
+op read x
@@
-echo last
+op read y
*** End Patch
"""
    assert list(guard.patch_files(patch, tmp_path)) == [
        (path, before, "echo same\n# choose\nop read x\necho keep\nop read y\n")
    ]


@pytest.mark.parametrize("wrapper", ["sh -c", "eval"])
def test_nested_heredocs(wrapper: str) -> None:
    text = f"{wrapper} 'bash <<EOF\nop read x\nEOF\n'"
    assert list(guard.shell_calls(text)) == [["op", "read", "x"]]


def test_pick_optional_fields() -> None:
    assert guard.pick({}, ("content",)) is None
    assert (
        guard.pick({"content": "first", "Content": "second"}, ("content", "Content"))
        == "first"
    )


@pytest.mark.parametrize(
    ("words", "expected"),
    [
        ([], []),
        (["op", "read", "x"], [["op", "read", "x"]]),
        (["ssh", "-o", "X=y", "host", "op", "read", "x"], [["op", "read", "x"]]),
        (["ssh", "host", "op read x"], [["op", "read", "x"]]),
        (["ssh", "host"], []),
        (["echo", "op"], []),
    ],
)
def test_direct_routes(words: list[str], expected: list[list[str]]) -> None:
    assert list(guard.direct_calls(words)) == expected


@pytest.mark.parametrize(
    ("words", "expected"),
    [
        (["op"], False),
        (["op", "--account", ACCOUNT], True),
        (["op", f"--account={ACCOUNT}"], True),
        (["op", "--account"], False),
        (["op", "--account", "wrong"], False),
        (["op", "--account=wrong"], False),
        (["op", "--account", ACCOUNT, "--account", "wrong"], False),
        (["op", "--account", ACCOUNT, f"--account={ACCOUNT}"], True),
        (["--account=/dir/op"], False),
    ],
)
def test_account_values(words: list[str], expected: bool) -> None:
    assert guard.valid_account(words, ACCOUNT) is expected


@pytest.mark.parametrize(
    ("words", "expected"),
    [
        (["op"], False),
        (["op", "read", "ref"], False),
        (["op", "--account", "expr"], True),
        (["op", "--account=expr"], True),
        (["op", "--accounts"], False),
        (["--account=/dir/op"], False),
    ],
)
def test_argv_flags(words: list[str], expected: bool) -> None:
    assert guard.argv_account(guard.Argv(words)) is expected


def test_markdown_closing_fence_stops_shell() -> None:
    text = "```sh\nop read x\n```\nop read prose\n```python\nop read python\n```"
    assert list(guard.markdown_calls(text)) == [["op", "read", "x"]]


def test_empty_patch() -> None:
    assert not list(guard.patch_files("*** Begin Patch\n*** End Patch", Path.cwd()))


@pytest.mark.parametrize("suffix", ["sh", "md"])
def test_patch_context_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    suffix: str,
) -> None:
    path = tmp_path / f"context.{suffix}"
    path.write_text(" op item get old\necho old\n")
    patch = (
        f"*** Begin Patch\n*** Update File: {path.name}\n@@\n"
        "  op item get old\n-echo old\n+echo new\n*** End Patch"
    )
    assert not invoke(
        monkeypatch,
        capsys,
        {
            "tool_name": "apply_patch",
            "cwd": str(tmp_path),
            "tool_input": {"command": patch},
        },
    )


@pytest.mark.parametrize(
    "content", [b"# caf\xe9\necho old\n", b"# caf\xe9\nop read old\n"]
)
def test_non_utf8_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    content: bytes,
) -> None:
    path = tmp_path / "latin.sh"
    path.write_bytes(content)
    assert invoke(
        monkeypatch,
        capsys,
        {
            "tool_name": "Write",
            "tool_input": {
                "file_path": str(path),
                "content": guard.read_file(path) + "op read new\n",
            },
        },
    )


@pytest.mark.parametrize("path_kind", ["missing", "directory"])
def test_unreadable_file(tmp_path: Path, path_kind: str) -> None:
    path = tmp_path / path_kind
    if path_kind == "directory":
        path.mkdir()
    assert guard.read_file(path) == ""


@pytest.mark.parametrize(
    "prefix",
    [
        "sudo -n",
        "sudo -k",
        "sudo -s",
        *[
            f"sudo {flag} value"
            for flag in ("-u", "-g", "-h", "-p", "-C", "-D", "-R", "-T", "-U")
        ],
        "nice -n 2",
        "timeout -k 1 5",
        "timeout -s TERM 5",
        "timeout 5 --",
        "env -u NAME",
        "env -C .",
        "env -S 'op read x'",
        "exec -a name",
        *[
            f"xargs {flag}{value}"
            for flag in ("-I", "-L", "-n", "-P", "-s", "-d", "-E", "-a")
            for value in ("value", " value")
        ],
        "ssh -t host --",
        "ssh host -t",
        "ssh host --",
        ">out",
        "> out",
        "2>/dev/null",
        "2> /dev/null",
        "2>&1",
        "2>& 1",
        "<in",
        "< in",
        "&>f",
        "&> f",
        "function f {",
        "find . -exec",
        "find . -execdir",
        "watch",
        "watch -n 2",
        "stdbuf -oL",
        "stdbuf -o L",
        "setsid",
        "ionice -c 3",
        "doas -u user",
        "flock -w 3 file",
    ],
)
def test_review_shell_shapes(prefix: str) -> None:
    text = prefix if prefix == "env -S 'op read x'" else prefix + " op read x"
    if prefix.startswith("find"):
        text += " \\;"
    if prefix == "function f {":
        text += "; }"
    assert list(guard.shell_calls(text)) == [["op", "read", "x"]]


@pytest.mark.parametrize(
    "text",
    [
        "command -v op",
        "command -V op",
        "env command -v op",
        "echo 'op read x'",
        "env -u NAME",
    ],
)
def test_lookup_exemption(text: str) -> None:
    assert not list(guard.shell_calls(text))


@pytest.mark.parametrize("text", ["op --version", "op signin"])
def test_all_op_commands_need_account(text: str) -> None:
    assert guard.Check().offenders(guard.shell_calls(text))


@pytest.mark.parametrize(
    "info", ["bash title=x", "{.bash}", "Bash", "sh-session", "shellscript"]
)
def test_fence_info(info: str) -> None:
    body = "$ op read x\nop output" if info == "sh-session" else "op read x"
    assert list(guard.markdown_calls(f"```{info}\n{body}\n```")) == [
        ["op", "read", "x"]
    ]


@pytest.mark.parametrize(
    "text",
    [
        "---\nname: prose\n---\ncommand: [op, read, x]",
        "script: [op read x]",
        "before_script: [op read x]",
        "after_script: [op read x]",
        "ansible.builtin.command: [op, read, x]",
    ],
)
def test_yaml_review_shapes(text: str) -> None:
    assert list(guard.file_calls(Path("x.yml"), text)) == [["op", "read", "x"]]


def test_yaml_script_non_string() -> None:
    assert not list(guard.yaml_calls({"script": [123]}))


@pytest.mark.parametrize(
    "source",
    [
        "x = !op read x",
        "%%script bash\nop read x",
        "%%script sh\nop read x",
        "%%script zsh\nop read x",
    ],
)
def test_notebook_review_shapes(source: str) -> None:
    assert list(guard.notebook_calls(book(source))) == [["op", "read", "x"]]


@pytest.mark.parametrize("mode", ["replace", "insert", "delete"])
def test_unknown_notebook_cell(mode: str) -> None:
    before = book("print(1)")
    assert (
        guard.notebook_edit(
            before,
            {"cell_id": "unknown", "edit_mode": mode, "new_source": "!op read x"},
        )
        == before
    )


@pytest.mark.parametrize(
    "tool", ["Bash", "shell", "run_command", "apply_patch", "Write", "unknown"]
)
def test_shell_tool_selection(
    tool: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    payload = (
        {"toolCall": {"name": tool, "args": {"CommandLine": "op read x"}}}
        if tool == "run_command"
        else {"tool_name": tool, "tool_input": {"command": "op read x"}}
    )
    assert bool(invoke(monkeypatch, capsys, payload)) == (
        tool in {"Bash", "shell", "run_command"}
    )


@pytest.mark.parametrize(
    "text",
    [
        "find . -exec op read x {} +",
        "find . -execdir sh -c 'op read x' \\;",
        "find . -exec echo safe \\; -exec op read x \\;",
        '>"file name" op read x',
        "<'file name' op read x",
        "env -S 'bash -c' 'op read x'",
    ],
)
def test_recursive_review_shapes(text: str) -> None:
    calls = list(guard.shell_calls(text))
    assert calls
    assert calls[0][:3] == ["op", "read", "x"]


@pytest.mark.parametrize(
    "text",
    [
        "function f op read x",
        "time -p op read x",
        "env -u NAME -S 'op read x'",
        "ssh -- -destination op read x",
        "function f { op read x; }",
        "if function f { op read x; }; then true; fi",
    ],
)
def test_mutation_prefix_regressions(text: str) -> None:
    assert list(guard.shell_calls(text)) == [["op", "read", "x"]]


@pytest.mark.parametrize("text", ["mise run -- op read x", "echo exec -- op read x"])
def test_only_mise_execution_launchers(text: str) -> None:
    assert not list(guard.shell_calls(text))


@pytest.mark.parametrize(
    "text",
    [
        "find . -exec op read x {} + -print",
        "find . -exec op read x",
        "find -exec op read x \\;",
        "find . -exec \\; -exec op read x \\;",
        "find . -print",
    ],
)
def test_find_clause_boundaries(text: str) -> None:
    expected: list[list[str]] = (
        []
        if text == "find . -print"
        else [["op", "read", "x", "{}"]]
        if "{}" in text
        else [["op", "read", "x"]]
    )
    assert list(guard.shell_calls(text)) == expected


@pytest.mark.parametrize("info", ["", "XBashX", "{XBashX}"])
def test_non_shell_fence_info(info: str) -> None:
    assert not list(guard.markdown_calls(f"```{info}\nop read x\n```"))


@pytest.mark.parametrize(
    "source",
    [
        "%%script bash -e\nop read x",
        "%%script sh -e\nop read x",
        "%%script zsh -e\nop read x",
        "X = !op read x",
    ],
)
def test_notebook_magic_arguments(source: str) -> None:
    assert list(guard.notebook_calls(book(source))) == [["op", "read", "x"]]

"""Tests for `tbhbdev secret`, against a fake Connect API on 127.0.0.1."""

from __future__ import annotations

import io
import sys
from typing import TYPE_CHECKING, cast

import pytest

from tbhbdev import cli, secret, secrets

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from conftest import FakeConnect

VALUE = "plain-value-never-shown"
PROG = "tbhbdev secret"


@pytest.fixture
def fake(
    connect: Callable[[], FakeConnect],
    config: Callable[[list[str]], None],
    monkeypatch: pytest.MonkeyPatch,
) -> FakeConnect:
    """The Connect API the command reaches, with an `ops` vault holding `db`."""
    server = connect()
    ops = server.vault("ops")
    server.vault("spare")
    server.item(ops, "db", password=VALUE, user="admin")
    config([server.url])
    monkeypatch.setenv(secrets.TEST_TOKEN_ENV, server.token)
    return server


def run(argv: list[str], stdin: str = "") -> int:
    sys.stdin = io.StringIO(stdin)
    try:
        return secret.main(argv, PROG)
    finally:
        sys.stdin = sys.__stdin__


def test_the_command_is_registered() -> None:
    assert cli.TOOLS["secret"] is secret.main


def test_read_prints_exactly_the_value(
    fake: FakeConnect, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run(["read", "op://ops/db/password"]) == 0
    assert capsys.readouterr() == (VALUE, "")
    assert fake.requests


def test_get_hides_values_without_reveal(
    fake: FakeConnect, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run(["get", "ops", "db"]) == 0
    out = capsys.readouterr().out
    assert out == (
        "title: db\nid: i1\nvault: ops\ncategory: LOGIN\nfields:\n"
        "  password (CONCEALED)\n  user (CONCEALED)\n"
    )
    assert fake.items


def test_get_reveal_shows_values(
    fake: FakeConnect, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run(["get", "ops", "db", "--reveal"]) == 0
    out = capsys.readouterr().out
    assert f"  password (CONCEALED): {VALUE}\n" in out
    assert fake.items


def test_list_one_vault_or_every_vault(
    fake: FakeConnect, capsys: pytest.CaptureFixture[str]
) -> None:
    fake.item("v2", "key", k="v")
    assert run(["list", "spare"]) == 0
    assert capsys.readouterr().out == "spare\tkey\tLOGIN\ti2\n"
    assert run(["list"]) == 0
    assert capsys.readouterr().out == "ops\tdb\tLOGIN\ti1\nspare\tkey\tLOGIN\ti2\n"


def test_create_reads_fields_from_stdin(
    fake: FakeConnect, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run(["create", "spare", "api"], '{"key": "k1"}') == 0
    assert capsys.readouterr().out == "created spare/api (i2)\n"
    assert fake.items["v2"]["i2"]["fields"] == [
        {"label": "key", "value": "k1", "type": "CONCEALED"}
    ]


def test_edit_reads_fields_from_a_file(
    fake: FakeConnect, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "fields.json"
    path.write_text('{"password": "new"}')
    assert run(["edit", "ops", "db", "--file", str(path)]) == 0
    assert capsys.readouterr().out == "edited ops/db (i1)\n"
    assert store_value(fake) == "new"


def store_value(fake: FakeConnect) -> object:
    fields = cast("list[dict[str, object]]", fake.items["v1"]["i1"]["fields"])
    return fields[0]["value"]


@pytest.mark.parametrize(
    "stdin", ["", "not json", "[]", "{}", '{"a": 1}', '{"a": "b", "c": null}']
)
def test_bad_fields_fail_without_quoting_the_input(
    fake: FakeConnect, capsys: pytest.CaptureFixture[str], stdin: str
) -> None:
    assert run(["edit", "ops", "db"], stdin) == 1
    err = capsys.readouterr().err
    assert err == (
        f"{PROG}: fields must be a non-empty JSON object of label to string value\n"
    )
    assert store_value(fake) == VALUE


def test_an_unreadable_fields_file_fails(
    fake: FakeConnect, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    missing = tmp_path / "missing.json"
    assert run(["create", "ops", "x", "--file", str(missing)]) == 1
    assert capsys.readouterr().err == (
        f"{PROG}: cannot read {missing}: No such file or directory\n"
    )
    assert len(fake.items["v1"]) == 1


def test_delete_needs_yes(
    fake: FakeConnect, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run(["delete", "ops", "db"]) == 2
    assert capsys.readouterr().err == f"{PROG}: delete needs --yes\n"
    assert "i1" in fake.items["v1"]
    assert run(["delete", "ops", "db", "--yes"]) == 0
    assert capsys.readouterr().out == "deleted ops/db\n"
    assert fake.items["v1"] == {}


def test_move(fake: FakeConnect, capsys: pytest.CaptureFixture[str]) -> None:
    assert run(["move", "ops", "db", "spare"]) == 0
    assert capsys.readouterr().out == "moved db from ops to spare (i2)\n"
    assert fake.items["v1"] == {}


def test_a_failure_prints_the_error_and_exits_1(
    fake: FakeConnect, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run(["read", "ops/nope/password"]) == 1
    assert capsys.readouterr() == ("", f"{PROG}: no item 'nope' in vault 'ops'\n")
    assert fake.requests


@pytest.mark.parametrize(("argv", "code"), [([], 2), (["--help"], 0), (["read"], 2)])
def test_usage(argv: list[str], code: int) -> None:
    assert run(argv) == code


COMMANDS = "{read,get,list,create,edit,delete,move}"
FIELDS_HELP = "JSON object of label to value: a file, or - for stdin (default)"
HELP = {
    "": f"""\
usage: {PROG} [-h] {COMMANDS} ...

Read and manage 1Password items.

positional arguments:
  {COMMANDS}
    read                print one field's value
    get                 show an item's metadata and field labels
    list                list items in a vault, or in every vault
    create              create a secure note from JSON fields
    edit                set or add an item's fields from JSON
    delete              delete an item
    move                move an item to another vault

options:
  -h, --help            show this help message and exit
""",
    "read": f"""\
usage: {PROG} read [-h] ref

positional arguments:
  ref         vault/item/field or op://vault/item/field

options:
  -h, --help  show this help message and exit
""",
    "get": f"""\
usage: {PROG} get [-h] [--reveal] vault item

positional arguments:
  vault
  item

options:
  -h, --help  show this help message and exit
  --reveal    show field values too
""",
    "create": f"""\
usage: {PROG} create [-h] [--file FILE] vault title

positional arguments:
  vault
  title

options:
  -h, --help   show this help message and exit
  --file FILE  {FIELDS_HELP}
""",
    "edit": f"""\
usage: {PROG} edit [-h] [--file FILE] vault item

positional arguments:
  vault
  item

options:
  -h, --help   show this help message and exit
  --file FILE  {FIELDS_HELP}
""",
    "delete": f"""\
usage: {PROG} delete [-h] [--yes] vault item

positional arguments:
  vault
  item

options:
  -h, --help  show this help message and exit
  --yes       really delete it
""",
}


@pytest.mark.usefixtures("plain")
@pytest.mark.parametrize("command", list(HELP))
def test_help_text(command: str, capsys: pytest.CaptureFixture[str]) -> None:
    assert run([command, "--help"] if command else ["--help"]) == 0
    assert capsys.readouterr() == (HELP[command], "")


@pytest.mark.usefixtures("plain")
def test_no_command_names_the_missing_argument(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert run([]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == (
        f"usage: {PROG} [-h] {COMMANDS} ...\n"
        f"{PROG}: error: the following arguments are required: command\n"
    )

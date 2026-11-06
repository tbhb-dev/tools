"""`tbhbdev secret`: read and manage 1Password items through tbhbdev.secrets.

`read` prints one value and nothing else, for `$(tbhbdev secret read ...)`.
`get` shows an item's metadata and field labels, and values only with
`--reveal`. `create` and `edit` take fields as a JSON object of label to
value, from stdin or `--file`, so no value ever sits in argv. `delete` needs
`--yes`.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import cast

from tbhbdev import runner, secrets


def _fields(source: str) -> dict[str, str]:
    """The JSON object of label to value in SOURCE, a path or - for stdin.

    Raises:
        SecretsError: SOURCE can't be read, or doesn't hold such an object.
            The message never quotes the input.
    """
    try:
        text = sys.stdin.read() if source == "-" else Path(source).read_text()
    except OSError as exc:
        message = f"cannot read {source}: {exc.strerror}"
        raise secrets.SecretsError(message) from exc
    try:
        data: object = json.loads(text)
    except ValueError:
        # Equivalent mutants: any value but a non-empty object fails below alike.
        data = None  # pragma: no mutate
    if not isinstance(data, dict) or not data:
        message = "fields must be a non-empty JSON object of label to string value"
        raise secrets.SecretsError(message)
    fields: dict[object, object] = data
    if not all(isinstance(value, str) for value in fields.values()):
        message = "fields must be a non-empty JSON object of label to string value"
        raise secrets.SecretsError(message)
    return cast(
        # Equivalent mutants: cast ignores its type at runtime, so a changed
        # or None type string returns fields alike. The pragma also hides
        # dropping the type, which mutmut can't split from the string.
        "dict[str, str]",  # pragma: no mutate
        fields,
    )


def _show(item: secrets.Item, *, reveal: bool) -> None:
    print(f"title: {item.title}")
    print(f"id: {item.id}")
    print(f"vault: {item.vault}")
    print(f"category: {item.category}")
    print("fields:")
    for entry in item.fields:
        value = f": {entry.value}" if reveal else ""
        print(f"  {entry.label} ({entry.type}){value}")


def _read(args: argparse.Namespace) -> int:
    sys.stdout.write(secrets.Store().read(args.ref))
    return 0


def _get(args: argparse.Namespace) -> int:
    _show(secrets.Store().get(args.vault, args.item), reveal=args.reveal)
    return 0


def _list(args: argparse.Namespace) -> int:
    for entry in secrets.Store().list_items(args.vault):
        print(f"{entry.vault}\t{entry.title}\t{entry.category}\t{entry.id}")
    return 0


def _create(args: argparse.Namespace) -> int:
    fields = _fields(args.file)
    item = secrets.Store().create(args.vault, args.title, fields)
    print(f"created {item.vault}/{item.title} ({item.id})")
    return 0


def _edit(args: argparse.Namespace) -> int:
    fields = _fields(args.file)
    item = secrets.Store().edit(args.vault, args.item, fields)
    print(f"edited {item.vault}/{item.title} ({item.id})")
    return 0


def _delete(args: argparse.Namespace) -> int:
    if not args.yes:
        print(f"{args.prog}: delete needs --yes", file=sys.stderr)
        return 2
    secrets.Store().delete(args.vault, args.item)
    print(f"deleted {args.vault}/{args.item}")
    return 0


def _move(args: argparse.Namespace) -> int:
    item = secrets.Store().move(args.vault, args.item, args.destination)
    print(f"moved {args.item} from {args.vault} to {item.vault} ({item.id})")
    return 0


def _parser(prog: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=prog, description="Read and manage 1Password items."
    )
    parser.set_defaults(prog=prog)
    sub = parser.add_subparsers(dest="command", required=True)
    command = sub.add_parser

    read = command("read", help="print one field's value")
    read.add_argument("ref", help="vault/item/field or op://vault/item/field")
    read.set_defaults(run=_read)

    get = command("get", help="show an item's metadata and field labels")
    get.add_argument("vault")
    get.add_argument("item")
    get.add_argument("--reveal", action="store_true", help="show field values too")
    get.set_defaults(run=_get)

    listing = command("list", help="list items in a vault, or in every vault")
    listing.add_argument("vault", nargs="?")
    listing.set_defaults(run=_list)

    fields_help = "JSON object of label to value: a file, or - for stdin (default)"
    create = command("create", help="create a secure note from JSON fields")
    create.add_argument("vault")
    create.add_argument("title")
    create.add_argument("--file", default="-", help=fields_help)
    create.set_defaults(run=_create)

    edit = command("edit", help="set or add an item's fields from JSON")
    edit.add_argument("vault")
    edit.add_argument("item")
    edit.add_argument("--file", default="-", help=fields_help)
    edit.set_defaults(run=_edit)

    delete = command("delete", help="delete an item")
    delete.add_argument("vault")
    delete.add_argument("item")
    delete.add_argument("--yes", action="store_true", help="really delete it")
    delete.set_defaults(run=_delete)

    move = command("move", help="move an item to another vault")
    move.add_argument("vault")
    move.add_argument("item")
    move.add_argument("destination")
    move.set_defaults(run=_move)
    return parser


def main(argv: list[str], prog: str) -> int:
    return runner.main(_parser(prog), argv, secrets.SecretsError)

"""Secrets for tbhb-dev tools, from 1Password Connect.

Every tool reaches secrets through this module, and nothing else calls
Connect or `op` for secrets at runtime. `read("vault/item/field")` returns one
value; `Store` gets, lists, creates, edits, deletes and moves items.

The Connect token comes from the current user's own store: on macOS the
dedicated agent keychain, on Linux the file `.config/tbhbdev/secrets/
connect-token` (LINUX_TOKEN_FILE, under `Path.home()`, never XDG_CONFIG_HOME,
because configuration management writes to that fixed path). The
file is mode 0600 in a mode 0700 directory, both owned by the user, and a
token that fails any of those checks is refused. Configuration management
is the only writer of the Linux token file. The endpoints come from the user
config's `[connect] endpoints` (see userconfig.py), which bootstrap writes on
each agent host, and are tried in order.

Nothing here logs, prints or puts a secret value or a token into an exception
message.
"""

from __future__ import annotations

import os
import platform
import stat
import subprocess  # nosec B404
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

import httpx
from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives import serialization
from onepasswordconnectsdk.client import Client
from onepasswordconnectsdk.config import ClientConfig
from onepasswordconnectsdk.utils import PathBuilder

from tbhbdev import userconfig
lazy from tbhbdev.alpha.agent import host_tools
lazy from tbhbdev.alpha.agent.model import Invalid

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

type Json = dict[str, object]

# Where the token lives in the agent account. On macOS, security(1) resolves
# the keychain name inside ~/Library/Keychains; the service and account are
# the item's attributes. They name the tool, not a deployment: a host needs
# them before it reads any config.
KEYCHAIN = "tbhbdev.keychain"
MACOS_KEYCHAIN_FILE = Path("Library/Keychains/tbhbdev.keychain-db")
SERVICE = "tbhbdev-connect"
ACCOUNT = "agent"
# On Linux the token is this file, relative to the home directory.
LINUX_TOKEN_FILE = Path(".config/tbhbdev/secrets/connect-token")
MAX_TOKEN_FILE = 1 << 16

# Tests only: the name of a variable holding a token to use instead of the
# keychain's. Nothing in production sets it.
TEST_TOKEN_ENV = "TBHBDEV_TEST_TOKEN"  # noqa: S105  # nosec B105

TIMEOUT = httpx.Timeout(10.0, connect=5.0)
KEYCHAIN_TIMEOUT = 10.0
CLIENT_ERROR = 400
NOT_FOUND = 404
SERVER_ERROR = 500
# The methods a second endpoint may repeat after the first may have received
# them. Any other, such as POST, could make a second item.
IDEMPOTENT = frozenset({"GET", "PUT", "DELETE"})
REF_PARTS = 3

# Connect assigns these when it stores an item; a copy for another vault
# leaves them out.
_ASSIGNED = frozenset(
    {"id", "vault", "version", "createdAt", "updatedAt", "lastEditedBy"}
)


class SecretsError(Exception):
    """A secrets operation failed. The message never holds a value or token."""


def _unsafe(found: os.stat_result) -> str | None:
    """What is wrong with the open token file FOUND, or None."""
    uid = os.geteuid()
    checks = (
        (not stat.S_ISREG(found.st_mode), "isn't a regular file"),
        (found.st_uid != uid, "isn't owned by this user"),
        (bool(found.st_mode & 0o077), "is accessible to its group or others"),
    )
    return next((problem for bad, problem in checks if bad), None)


def _linux_token() -> str:
    """The Connect token in the current user's 0600 file.

    The final directory and file components cannot be symlinks. Ancestor
    directories and later permission changes are outside these checks.
    """
    path = Path.home() / LINUX_TOKEN_FILE
    fix = "have configuration management install it"
    try:
        directory = os.open(
            path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
        )
        try:
            found = os.fstat(directory)
            if found.st_uid != os.geteuid() or found.st_mode & 0o077:
                message = f"the Connect token directory {path.parent} is unsafe; {fix}"
                raise SecretsError(message)
            fd = os.open(
                path.name,
                os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                dir_fd=directory,
            )
            try:
                problem = _unsafe(os.fstat(fd))
                if problem:
                    message = f"the Connect token file {path} {problem}; {fix}"
                    raise SecretsError(message)
                data = os.read(fd, MAX_TOKEN_FILE + 1)
            finally:
                os.close(fd)
        finally:
            os.close(directory)
    except OSError as exc:
        message = (
            f"the Connect token file {path} can't be opened: {exc.strerror}; {fix}"
        )
        raise SecretsError(message) from None
    if len(data) > MAX_TOKEN_FILE:
        message = f"the Connect token file {path} exceeds {MAX_TOKEN_FILE} bytes; {fix}"
        raise SecretsError(message)
    value = data.decode(errors="replace").strip()
    if not value:
        message = f"the Connect token file {path} is empty; {fix}"
        raise SecretsError(message)
    return value


def token() -> str:
    """The Connect token in the current user's keychain or token file.

    Raises:
        SecretsError: There is no usable token, or the keychain tool isn't
            installed.
    """
    override = os.environ.get(TEST_TOKEN_ENV)
    if override:
        return override
    if platform.system() != "Darwin":
        return _linux_token()
    try:
        security = host_tools.resolve("security")
        # The keychain has no password, so unlocking it is idempotent and
        # holds no secret. It is locked again after every restart.
        subprocess.run(  # nosec B603 B607
            [security, "unlock-keychain", "-p", "", KEYCHAIN],
            capture_output=True,
            timeout=KEYCHAIN_TIMEOUT,
            # False is the default, written out for ruff. The pragma hides the
            # equivalent check=None and dropped-check mutants, not check=True.
            check=(  # pragma: no mutate
                False
            ),
        )
        argv = [security, "find-generic-password", "-s", SERVICE, "-a", ACCOUNT]
        argv += ["-w", KEYCHAIN]
        done = subprocess.run(  # nosec B603
            argv,
            capture_output=True,
            text=True,
            timeout=KEYCHAIN_TIMEOUT,
            # False is the default, written out for ruff. The pragma hides the
            # equivalent check=None and dropped-check mutants, not check=True.
            check=(  # pragma: no mutate
                False
            ),
        )
    except subprocess.TimeoutExpired:
        message = f"the keychain read timed out after {KEYCHAIN_TIMEOUT} seconds"
        raise SecretsError(message) from None
    except FileNotFoundError, Invalid:
        message = "can't read the Connect token: security isn't installed"
        raise SecretsError(message) from None
    value = done.stdout.rstrip("\n")
    if done.returncode != 0 or not value:
        message = (
            "no Connect token in this user's keychain; "
            "run `tbhbdev host bootstrap` for this host"
        )
        raise SecretsError(message)
    return value


def endpoints() -> list[str]:
    """The user config's `[connect] endpoints`, in failover order.

    On an agent host, `tbhbdev host bootstrap` writes them into the config.

    Raises:
        SecretsError: The config doesn't list any.
    """
    try:
        return userconfig.strings("connect", "endpoints")
    except userconfig.ConfigError as exc:
        message = f"{exc}; run `tbhbdev host bootstrap` for this host"
        raise SecretsError(message) from exc


class Backend(Protocol):
    """Where items live. Connect is the one backend today.

    Vaults and items are Connect's JSON objects, so an edit keeps every field
    of the item it doesn't touch.
    """

    def vaults(self) -> list[Json]: ...
    def items(self, vault_id: str) -> list[Json]: ...
    def item(self, vault_id: str, item_id: str) -> Json: ...
    def create(self, vault_id: str, item: Json) -> Json: ...
    def update(self, vault_id: str, item_id: str, item: Json) -> Json: ...
    def delete(self, vault_id: str, item_id: str) -> None: ...


def _object(value: object) -> Json:
    if not isinstance(value, dict):
        message = "Connect sent an unexpected response"
        raise SecretsError(message)
    return value


def _objects(value: object) -> list[Json]:
    if not isinstance(value, list):
        message = "Connect sent an unexpected response"
        raise SecretsError(message)
    entries: list[object] = value
    return [_object(entry) for entry in entries]


def _text(value: Json, key: str) -> str:
    entry = value.get(key)
    return entry if isinstance(entry, str) else ""


class Connect:
    """The Connect API at each endpoint in turn, through 1Password's SDK.

    A failure to connect moves on to the next endpoint. So does any other
    connection error or a 5xx answer to a GET, PUT or DELETE, but not to a
    POST, which the endpoint may have done already. A 4xx answer is final,
    except that a DELETE that finds nothing once an endpoint may have done it
    has succeeded. The endpoint that last answered is tried first. Neither
    its repr nor its errors show a URL, which holds the tailnet's suffix.
    """

    def __init__(self, token: str, urls: Sequence[str]) -> None:
        self._token = token
        self._urls = list(urls)
        self._clients: dict[str, Client] = {}

    def __repr__(self) -> str:
        return f"Connect(endpoints={len(self._urls)})"

    def _client(self, url: str) -> Client:
        if url not in self._clients:
            # A Client whose constructor fails raises again as it's deleted,
            # so a URL httpx refuses never reaches one.
            httpx.URL(url)
            config = ClientConfig(timeout=TIMEOUT)
            self._clients[url] = Client(url, self._token, config)
        return self._clients[url]

    def _send(
        self, url: str, method: str, path: str, body: Json | None
    ) -> tuple[httpx.Response | None, bool]:
        """The endpoint's answer, or None to move on to the next one.

        The flag says whether the endpoint may have done the request already.
        """
        try:
            response = self._client(url).build_request(method, path, body)
        except httpx.ConnectError, httpx.ConnectTimeout:
            return None, False
        except httpx.TransportError as exc:
            if method in IDEMPOTENT:
                return None, True
            kind = type(exc).__name__
            message = f"Connect may have done {method} {path}: {kind}"
            raise SecretsError(message) from None
        # Any other failure to build or send the request is final. Its
        # message can hold the URL, so neither it nor its traceback shows.
        except (
            httpx.HTTPError,
            httpx.InvalidURL,
            httpx.StreamError,
            UnicodeError,
        ) as exc:
            message = f"Connect can't send {method} {path}: {type(exc).__name__}"
            raise SecretsError(message) from None
        status = response.status_code
        if status < SERVER_ERROR:
            return response, False
        if method in IDEMPOTENT:
            return None, True
        message = f"Connect failed {method} {path}: HTTP {status}"
        raise SecretsError(message)

    def _call(self, method: str, path: str, body: Json | None = None) -> object:
        # Whether an endpoint passed over may have done the request already.
        # None is false as well, so the pragma hides an equivalent mutant.
        repeated = False  # pragma: no mutate
        for url in list(self._urls):
            response, done = self._send(url, method, path, body)
            repeated = repeated or done
            if response is None:
                continue
            status = response.status_code
            # A DELETE an endpoint passed over may have done finds nothing.
            if repeated and method == "DELETE" and status == NOT_FOUND:
                return None
            if status >= CLIENT_ERROR:
                message = f"Connect refused {method} {path}: HTTP {status}"
                raise SecretsError(message)
            self._urls.remove(url)
            self._urls.insert(0, url)
            if not response.content:
                return None
            try:
                data: object = response.json()
            except ValueError as exc:
                message = f"Connect sent an unexpected response to {method} {path}"
                raise SecretsError(message) from exc
            return data
        message = f"no Connect endpoint answered {method} {path}"
        raise SecretsError(message)

    def vaults(self) -> list[Json]:
        return _objects(self._call("GET", PathBuilder().vaults().build()))

    def items(self, vault_id: str) -> list[Json]:
        path = PathBuilder().vaults(vault_id).items().build()
        return _objects(self._call("GET", path))

    def item(self, vault_id: str, item_id: str) -> Json:
        path = PathBuilder().vaults(vault_id).items(item_id).build()
        return _object(self._call("GET", path))

    def create(self, vault_id: str, item: Json) -> Json:
        path = PathBuilder().vaults(vault_id).items().build()
        return _object(self._call("POST", path, item))

    def update(self, vault_id: str, item_id: str, item: Json) -> Json:
        path = PathBuilder().vaults(vault_id).items(item_id).build()
        return _object(self._call("PUT", path, item))

    def delete(self, vault_id: str, item_id: str) -> None:
        path = PathBuilder().vaults(vault_id).items(item_id).build()
        self._call("DELETE", path)


def connect() -> Connect:
    """Connect with this user's token, at the configured endpoints."""
    return Connect(token(), endpoints())


@dataclass(frozen=True)
class Field:
    """One field of an item. Its value stays out of repr."""

    id: str
    label: str
    type: str
    value: str = field(repr=False)


@dataclass(frozen=True)
class Item:
    id: str
    title: str
    category: str
    vault_id: str
    vault: str
    fields: tuple[Field, ...]


@dataclass(frozen=True)
class Summary:
    id: str
    title: str
    category: str
    vault: str


def parse_ref(ref: str) -> tuple[str, str, str]:
    """Split `vault/item/field` or `op://vault/item/field`.

    Strip the optional `?ssh-format=openssh` query and refuse any other query.

    Raises:
        SecretsError: REF has another shape.
    """
    path, separator, query = ref.partition("?")
    if separator and query != "ssh-format=openssh":
        message = f"{ref!r} has an unsupported reference query"
        raise SecretsError(message)
    parts = path.removeprefix("op://").split("/")
    if len(parts) != REF_PARTS or not all(parts):
        message = (
            f"{ref!r} is not a vault/item/field or op://vault/item/field reference"
        )
        raise SecretsError(message)
    return parts[0], parts[1], parts[2]


def _openssh(value: str, label: str) -> str:
    """Convert a Connect PEM value in memory without exposing conversion errors."""
    try:
        key = serialization.load_pem_private_key(value.encode(), password=None)
        return key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.OpenSSH,
            serialization.NoEncryption(),
        ).decode()
    except ValueError, TypeError, UnsupportedAlgorithm:
        message = f"field {label!r} cannot convert to OpenSSH private key"
        raise SecretsError(message) from None


def _fields(raw: Json) -> list[Json]:
    entries = raw.get("fields")
    return _objects(entries) if entries is not None else []


def _one(matches: list[Json], what: str) -> Json:
    if not matches:
        message = f"no {what}"
        raise SecretsError(message)
    if len(matches) > 1:
        message = f"more than one {what}"
        raise SecretsError(message)
    return matches[0]


def _new_field(label: str, value: str) -> Json:
    return {"label": label, "value": value, "type": "CONCEALED"}


class Store:
    """Items in the vaults the token can reach, by vault and item name or ID."""

    def __init__(self, backend: Backend | None = None) -> None:
        self._backend = backend or connect()

    def _vault(self, ref: str) -> tuple[str, str]:
        vaults = self._backend.vaults()
        match = _one(
            [v for v in vaults if ref in {_text(v, "id"), _text(v, "name")}],
            f"vault {ref!r}",
        )
        return _text(match, "id"), _text(match, "name")

    def _titled(self, vault_id: str, ref: str) -> list[Json]:
        return [
            entry
            for entry in self._backend.items(vault_id)
            if ref in {_text(entry, "id"), _text(entry, "title")}
        ]

    def _item_id(self, vault_id: str, vault: str, ref: str) -> str:
        match = _one(self._titled(vault_id, ref), f"item {ref!r} in vault {vault!r}")
        return _text(match, "id")

    def _absent(self, vault_id: str, vault: str, title: str) -> None:
        if self._titled(vault_id, title):
            message = f"vault {vault!r} already has an item titled {title!r}"
            raise SecretsError(message)

    def _item(self, vault_id: str, vault: str, raw: Json) -> Item:
        fields = tuple(
            Field(
                _text(entry, "id"),
                _text(entry, "label"),
                _text(entry, "type"),
                _text(entry, "value"),
            )
            for entry in _fields(raw)
        )
        return Item(
            _text(raw, "id"),
            _text(raw, "title"),
            _text(raw, "category"),
            vault_id,
            vault,
            fields,
        )

    def read(self, ref: str) -> str:
        """The value of the field a `vault/item/field` reference names."""
        vault_ref, item_ref, field_ref = parse_ref(ref)
        vault_id, vault = self._vault(vault_ref)
        raw = self._backend.item(vault_id, self._item_id(vault_id, vault, item_ref))
        match = _one(
            [
                f
                for f in _fields(raw)
                if field_ref in {_text(f, "id"), _text(f, "label")}
            ],
            f"field {field_ref!r} in item {item_ref!r}",
        )
        value = _text(match, "value")
        return _openssh(value, field_ref) if "?" in ref else value

    def get(self, vault: str, item: str) -> Item:
        vault_id, name = self._vault(vault)
        raw = self._backend.item(vault_id, self._item_id(vault_id, name, item))
        return self._item(vault_id, name, raw)

    def list_items(self, vault: str | None = None) -> list[Summary]:
        """The items in VAULT, or in every vault the token can reach."""
        if vault is None:
            vaults = [
                (_text(v, "id"), _text(v, "name")) for v in self._backend.vaults()
            ]
        else:
            vaults = [self._vault(vault)]
        return [
            Summary(
                _text(entry, "id"),
                _text(entry, "title"),
                _text(entry, "category"),
                name,
            )
            for vault_id, name in vaults
            for entry in self._backend.items(vault_id)
        ]

    def create(self, vault: str, title: str, fields: Mapping[str, str]) -> Item:
        """A new secure note titled TITLE, with one concealed field per entry."""
        vault_id, name = self._vault(vault)
        self._absent(vault_id, name, title)
        body: Json = {
            "title": title,
            "category": "SECURE_NOTE",
            "vault": {"id": vault_id},
            "fields": [_new_field(label, value) for label, value in fields.items()],
        }
        return self._item(vault_id, name, self._backend.create(vault_id, body))

    def edit(self, vault: str, item: str, fields: Mapping[str, str]) -> Item:
        """Set each labeled field's value; a new label adds a concealed field."""
        vault_id, name = self._vault(vault)
        item_id = self._item_id(vault_id, name, item)
        raw = self._backend.item(vault_id, item_id)
        entries = _fields(raw)
        for label, value in fields.items():
            labeled = [f for f in entries if _text(f, "label") == label]
            if len(labeled) > 1:
                message = f"item {item!r} has more than one field labeled {label!r}"
                raise SecretsError(message)
            if labeled:
                labeled[0]["value"] = value
            else:
                entries.append(_new_field(label, value))
        raw["fields"] = entries
        return self._item(vault_id, name, self._backend.update(vault_id, item_id, raw))

    def delete(self, vault: str, item: str) -> None:
        vault_id, name = self._vault(vault)
        self._backend.delete(vault_id, self._item_id(vault_id, name, item))

    def move(self, vault: str, item: str, destination: str) -> Item:
        """Create a copy of ITEM in DESTINATION, then delete the original.

        When either step fails, the copy is deleted again, so the item stays
        in VAULT and nothing is left behind in DESTINATION. A failed delete
        that removed the item anyway still counts as a move. When the move
        can't tell whether the delete happened, it keeps the copy.

        Raises:
            SecretsError: The move failed. The message says whether a copy
                could remain.
        """
        src_id, src = self._vault(vault)
        dst_id, dst = self._vault(destination)
        item_id = self._item_id(src_id, src, item)
        raw = self._backend.item(src_id, item_id)
        if raw.get("files"):
            message = f"item {item!r} has files, which Connect can't copy"
            raise SecretsError(message)
        title = _text(raw, "title")
        self._absent(dst_id, dst, title)
        copy = {key: value for key, value in raw.items() if key not in _ASSIGNED}
        copy["vault"] = {"id": dst_id}
        try:
            created = self._backend.create(dst_id, copy)
        except SecretsError:
            self._discard(dst_id, dst, title)
            raise
        try:
            self._backend.delete(src_id, item_id)
        except SecretsError:
            # A delete can fail after Connect has done it, and the copy is
            # then the only one left.
            try:
                # By id alone: another item may have this id as its title.
                listed = self._backend.items(src_id)
                kept = any(_text(entry, "id") == item_id for entry in listed)
            except SecretsError as exc:
                message = (
                    f"the move couldn't tell whether it deleted item {item!r}"
                    f" from vault {vault!r}, so it kept the copy in vault"
                    f" {destination!r}"
                )
                raise SecretsError(message) from exc
            if kept:
                self._discard(dst_id, dst, title)
                raise
        return self._item(dst_id, dst, created)

    def _discard(self, vault_id: str, vault: str, title: str) -> None:
        """Delete every item titled TITLE: the copy a failed move made.

        Connect may store a copy even when the request that made it fails. The
        destination had no item with this title before the move, so any there
        now is the move's.
        """
        try:
            for entry in self._titled(vault_id, title):
                self._backend.delete(vault_id, _text(entry, "id"))
        except SecretsError as exc:
            message = f"the move failed, and its copy may remain in vault {vault!r}"
            raise SecretsError(message) from exc


def read(ref: str) -> str:
    """The value a `vault/item/field` or `op://vault/item/field` reference names."""
    return Store().read(ref)

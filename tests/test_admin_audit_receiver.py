"""The audit receiver: wire contract, validation, durability and quota."""

import hashlib
import io
import os
from pathlib import Path

import pytest

from tbhbdev.admin import audit_receiver as receiver

GENESIS = "0" * 64
SEGMENT = 70000
QUOTA = 200000


def digest(line: bytes) -> str:
    return hashlib.sha256(line.removesuffix(b"\n")).hexdigest()


def fresh(tmp_path: Path) -> Path:
    directory = tmp_path / "receiver"
    directory.mkdir(mode=0o700)
    (directory / "segment-1.jsonl").write_bytes(b"")
    (directory / "head").write_text(f"1 0 {GENESIS} 0\n")
    return directory


def run(
    directory: Path,
    command: str | None,
    data: bytes = b"",
    *,
    segment: int = SEGMENT,
    quota: int = QUOTA,
) -> tuple[int, bytes]:
    out = io.BytesIO()
    code = receiver.serve(
        str(directory), segment, quota, command, io.BytesIO(data), out
    )
    return code, out.getvalue()


def record(prev: str, extra: str = "") -> bytes:
    return f'{{"prev": "{prev}"{extra}}}\n'.encode()


def head_of(directory: Path) -> list[str]:
    return (directory / "head").read_text().split()


def test_head_of_a_fresh_receiver(tmp_path: Path) -> None:
    directory = fresh(tmp_path)
    assert run(directory, "head") == (0, f"ack 1 0 {GENESIS}\n".encode())


@pytest.mark.parametrize(
    "command",
    [
        None,
        "",
        "heads",
        "head ",
        "head 1",
        "append",
        "append 0",
        "append 01",
        "append -1",
        "append 1 2",
        "append  1",
        "APPEND 1",
        "append 1\n",
        "head\nappend 1",
    ],
)
def test_other_commands_exit_nonzero_with_empty_output(
    tmp_path: Path, command: str | None
) -> None:
    directory = fresh(tmp_path)
    assert run(directory, command, record(GENESIS)) == (1, b"")


def test_head_refuses_any_input(tmp_path: Path) -> None:
    directory = fresh(tmp_path)
    assert run(directory, "head", b"x") == (1, b"")


def test_append_adds_one_line_and_acknowledges_it(tmp_path: Path) -> None:
    directory = fresh(tmp_path)
    line = record(GENESIS, ', "a": 1')
    code, out = run(directory, "append 1", line)
    assert (code, out) == (0, f"ack 1 1 {digest(line)}\n".encode())
    assert (directory / "segment-1.jsonl").read_bytes() == line
    assert head_of(directory) == ["1", "1", digest(line), str(len(line))]
    second = record(digest(line))
    assert run(directory, "append 2", second)[0] == 0
    assert (directory / "segment-1.jsonl").read_bytes() == line + second
    assert run(directory, "head")[1] == f"ack 1 2 {digest(second)}\n".encode()


def test_a_replay_of_the_newest_line_needs_that_line(tmp_path: Path) -> None:
    directory = fresh(tmp_path)
    line = record(GENESIS)
    run(directory, "append 1", line)
    assert run(directory, "append 1", line) == (
        0,
        f"ack 1 1 {digest(line)}\n".encode(),
    )
    other = record("f" * 64, ', "different": true')
    assert run(directory, "append 1", other) == (1, b"")
    assert (directory / "segment-1.jsonl").read_bytes() == line


def test_a_replay_of_an_older_line_answers_with_the_head_and_writes_nothing(
    tmp_path: Path,
) -> None:
    directory = fresh(tmp_path)
    line = record(GENESIS)
    second = record(digest(line))
    run(directory, "append 1", line)
    run(directory, "append 2", second)
    other = record("f" * 64, ', "different": true')
    assert run(directory, "append 1", other) == (
        0,
        f"ack 1 2 {digest(second)}\n".encode(),
    )
    assert (directory / "segment-1.jsonl").read_bytes() == line + second


def test_an_older_replay_still_checks_size_and_byte_range_but_not_json(
    tmp_path: Path,
) -> None:
    directory = fresh(tmp_path)
    line = record(GENESIS)
    run(directory, "append 1", line)
    run(directory, "append 2", record(digest(line)))
    assert run(directory, "append 1", b"not json at all\n")[0] == 0
    assert run(directory, "append 1", b"caf\xc3\xa9\n") == (1, b"")
    assert run(directory, "append 1", b"x" * 65536 + b"\n") == (1, b"")


def test_a_position_past_the_next_one_is_refused(tmp_path: Path) -> None:
    directory = fresh(tmp_path)
    assert run(directory, "append 2", record(GENESIS)) == (1, b"")
    assert head_of(directory)[1] == "0"


@pytest.mark.parametrize(
    "line",
    [
        b"",
        b"\n",
        b'{"prev": "' + b"0" * 64 + b'"}',
        b'{"prev": "' + b"0" * 64 + b'"}\n\n',
        b'{"prev": "' + b"0" * 64 + b'"}\n{}\n',
        b'{"prev": "' + b"0" * 64 + b'", "a": "\xc3\xa9"}\n',
        b'{"prev": "' + b"0" * 64 + b'", "a": "\t"}\n',
        b'{"prev": "' + b"0" * 64 + b'", "a": "\x7f"}\n',
        b"[]\n",
        b'"x"\n',
        b"{}\n",
        b'{"prev": 1}\n',
        b'{"prev": "' + b"0" * 63 + b'"}\n',
        b'{"prev": "' + b"A" * 64 + b'"}\n',
        b'{"prev": "' + b"1" * 64 + b'"}\n',
        b'{"prev": "' + b"0" * 64 + b'", "n": NaN}\n',
        b'{"prev": "' + b"0" * 64 + b'", "n": Infinity}\n',
        b'{"prev": "' + b"0" * 64 + b'", "n": -Infinity}\n',
        b'{"prev": "' + b"0" * 64 + b'", "a": 1, "a": 2}\n',
        b'{"prev": "' + b"0" * 64 + b'", "o": {"b": 1, "b": 2}}\n',
        b'{"prev": "' + b"0" * 64 + b'", "o": [{"b": 1, "b": 2}]}\n',
        b'{"prev": "' + b"0" * 64 + b'",}\n',
        b"{" * 100000 + b"\n",
        b"x" * 65536 + b"\n",
    ],
)
def test_invalid_lines_are_refused_and_change_nothing(
    tmp_path: Path, line: bytes
) -> None:
    directory = fresh(tmp_path)
    assert run(directory, "append 1", line) == (1, b"")
    assert (directory / "segment-1.jsonl").read_bytes() == b""
    assert head_of(directory) == ["1", "0", GENESIS, "0"]


def test_the_largest_valid_line_is_accepted(tmp_path: Path) -> None:
    directory = fresh(tmp_path)
    prefix = b'{"prev": "' + b"0" * 64 + b'", "pad": "'
    line = prefix + b"x" * (65536 - len(prefix) - 3) + b'"}\n'
    assert len(line) == 65536
    assert run(directory, "append 1", line)[0] == 0
    too_long = prefix + b"x" * (65537 - len(prefix) - 3) + b'"}\n'
    assert run(directory, "append 1", too_long) == (1, b"")


def test_input_past_one_line_is_refused(tmp_path: Path) -> None:
    directory = fresh(tmp_path)
    data = record(GENESIS) + b"x" * 70000
    assert run(directory, "append 1", data) == (1, b"")


def test_decimal_digits_only_name_a_position(tmp_path: Path) -> None:
    directory = fresh(tmp_path)
    assert run(directory, "append " + chr(0x661), record(GENESIS)) == (1, b"")


def test_a_missing_or_malformed_head_file_refuses(tmp_path: Path) -> None:
    directory = fresh(tmp_path)
    for text in (
        "",
        "1 0 " + GENESIS + "\n",
        "1 0 " + GENESIS + " 0",
        "0 0 " + GENESIS + " 0\n",
        "1 -1 " + GENESIS + " 0\n",
        "1 00 " + GENESIS + " 0\n",
        "1 0 " + "g" * 64 + " 0\n",
        "1 0 " + GENESIS + " 00\n",
        "1 0 " + GENESIS + " x\n",
        "1 0 " + GENESIS + " 0 9\n",
    ):
        (directory / "head").write_text(text)
        assert run(directory, "head") == (1, b"")
    (directory / "head").unlink()
    assert run(directory, "head") == (1, b"")


def test_a_head_file_that_is_not_text_refuses(tmp_path: Path) -> None:
    directory = fresh(tmp_path)
    (directory / "head").write_bytes(b"\xff\xfe\n")
    assert run(directory, "head") == (1, b"")


def test_recovery_truncates_a_longer_segment_and_keeps_an_equal_one(
    tmp_path: Path,
) -> None:
    directory = fresh(tmp_path)
    line = record(GENESIS)
    run(directory, "append 1", line)
    segment = directory / "segment-1.jsonl"
    segment.write_bytes(line + b'{"partial')
    assert run(directory, "head")[0] == 0
    assert segment.read_bytes() == line
    assert run(directory, "head")[0] == 0
    assert segment.read_bytes() == line


def test_a_shorter_segment_refuses_without_truncation(tmp_path: Path) -> None:
    directory = fresh(tmp_path)
    line = record(GENESIS)
    run(directory, "append 1", line)
    segment = directory / "segment-1.jsonl"
    segment.write_bytes(line[:10])
    assert run(directory, "head") == (1, b"")
    assert run(directory, "append 2", record(digest(line))) == (1, b"")
    assert segment.read_bytes() == line[:10]


def test_a_missing_current_segment_refuses(tmp_path: Path) -> None:
    directory = fresh(tmp_path)
    (directory / "segment-1.jsonl").unlink()
    assert run(directory, "head") == (1, b"")


def test_recovery_removes_a_temporary_head_and_later_segments(
    tmp_path: Path,
) -> None:
    directory = fresh(tmp_path)
    (directory / "head.tmp").write_text("junk")
    (directory / "segment-2.jsonl").write_bytes(b"orphan")
    (directory / "segment-10.jsonl").write_bytes(b"orphan")
    (directory / "notes.txt").write_text("keep")
    (directory / "segment-x.jsonl").write_text("keep")
    assert run(directory, "head")[0] == 0
    names = sorted(p.name for p in directory.iterdir())
    assert names == ["head", "lock", "notes.txt", "segment-1.jsonl", "segment-x.jsonl"]


def test_a_full_segment_rotates_with_the_chain_continuing(tmp_path: Path) -> None:
    directory = fresh(tmp_path)
    first = record(GENESIS, ', "pad": "' + "x" * 40 + '"')
    second = record(digest(first), ', "pad": "' + "y" * 40 + '"')
    size = len(first) + len(second) - 1
    assert run(directory, "append 1", first, segment=size)[0] == 0
    assert run(directory, "append 2", second, segment=size)[0] == 0
    assert (directory / "segment-1.jsonl").read_bytes() == first
    assert (directory / "segment-2.jsonl").read_bytes() == second
    assert head_of(directory) == ["2", "2", digest(second), str(len(second))]


def test_a_line_that_just_fits_does_not_rotate(tmp_path: Path) -> None:
    directory = fresh(tmp_path)
    first = record(GENESIS)
    second = record(digest(first))
    size = len(first) + len(second)
    run(directory, "append 1", first, segment=size)
    run(directory, "append 2", second, segment=size)
    assert not (directory / "segment-2.jsonl").exists()


def test_rotation_truncates_a_leftover_file_of_the_new_name(tmp_path: Path) -> None:
    directory = fresh(tmp_path)
    first = record(GENESIS, ', "pad": "' + "x" * 40 + '"')
    run(directory, "append 1", first, segment=len(first))
    second = record(digest(first))
    (directory / "segment-2.jsonl").write_bytes(b"stale")
    # The stale file is removed by recovery first, then created empty.
    assert run(directory, "append 2", second, segment=len(first))[0] == 0
    assert (directory / "segment-2.jsonl").read_bytes() == second


def test_a_full_quota_refuses_an_append_but_answers_head_and_replays(
    tmp_path: Path,
) -> None:
    directory = fresh(tmp_path)
    first = record(GENESIS)
    quota = len(first) + len(record(digest(first))) - 1
    assert run(directory, "append 1", first, quota=quota)[0] == 0
    assert run(directory, "append 2", record(digest(first)), quota=quota) == (1, b"")
    assert run(directory, "head", quota=quota)[0] == 0
    assert run(directory, "append 1", first, quota=quota)[0] == 0
    assert not (directory / "segment-2.jsonl").exists()


def test_the_quota_counts_every_segment_and_the_rotation_case(
    tmp_path: Path,
) -> None:
    directory = fresh(tmp_path)
    first = record(GENESIS, ', "pad": "' + "x" * 40 + '"')
    second = record(digest(first))
    size = len(first)
    quota = len(first) + len(second) - 1
    run(directory, "append 1", first, segment=size, quota=quota)
    assert run(directory, "append 2", second, segment=size, quota=quota) == (1, b"")
    assert not (directory / "segment-2.jsonl").exists()
    assert head_of(directory)[0] == "1"
    exact = len(first) + len(second)
    assert run(directory, "append 2", second, segment=size, quota=exact)[0] == 0


def test_a_failed_sync_ends_the_request_without_an_acknowledgment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = fresh(tmp_path)

    def broken(_descriptor: int) -> None:
        raise OSError

    monkeypatch.setattr(os, "fsync", broken)
    assert run(directory, "head") == (1, b"")
    assert run(directory, "append 1", record(GENESIS)) == (1, b"")


def test_a_failed_write_leaves_the_head_alone_and_the_next_request_recovers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = fresh(tmp_path)
    line = record(GENESIS)
    real = os.replace

    def broken(*_args: object, **_kwargs: object) -> None:
        raise OSError

    monkeypatch.setattr(os, "replace", broken)
    assert run(directory, "append 1", line) == (1, b"")
    monkeypatch.setattr(os, "replace", real)
    assert (directory / "segment-1.jsonl").read_bytes() == line
    assert head_of(directory) == ["1", "0", GENESIS, "0"]
    assert run(directory, "head")[0] == 0
    assert (directory / "segment-1.jsonl").read_bytes() == b""
    assert not (directory / "head.tmp").exists()


def test_the_lock_is_held_for_the_whole_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = fresh(tmp_path)
    import fcntl  # noqa: PLC0415

    seen: list[int] = []
    real = fcntl.flock

    def spy(descriptor: int, operation: int) -> None:
        seen.append(operation)
        real(descriptor, operation)

    monkeypatch.setattr(fcntl, "flock", spy)
    run(directory, "head")
    assert seen == [fcntl.LOCK_EX]


def test_a_missing_directory_refuses(tmp_path: Path) -> None:
    assert run(tmp_path / "nothing", "head") == (1, b"")


def test_main_checks_the_arguments_then_serves(tmp_path: Path) -> None:
    directory = fresh(tmp_path)
    out = io.BytesIO()
    bad: list[list[str]] = [
        [],
        [str(directory)],
        [str(directory), "x", "1"],
        [str(directory), "\u0661", "131072"],
        [str(directory), "65535", "131072"],
        [str(directory), "65536", "131071"],
        [str(directory), "65536", "131072", "extra"],
    ]
    for argv in bad:
        assert receiver.main(argv, "head", io.BytesIO(), out) == 1
    assert out.getvalue() == b""
    good = [str(directory), "65536", "131072"]
    assert receiver.main(good, "head", io.BytesIO(), out) == 0
    assert out.getvalue() == f"ack 1 0 {GENESIS}\n".encode()


STANDARD = {
    "__future__",
    "fcntl",
    "hashlib",
    "json",
    "os",
    "pathlib",
    "re",
    "stat",
    "sys",
    "typing",
}


def foreign_imports(source: str, path: Path) -> set[str]:
    imports = {
        line.split()[1].split(".")[0]
        for line in source.splitlines()
        if line.startswith(("import ", "from "))
    }
    if "mutants" in path.parts:
        # The mutation runner's trampoline adds this import to a mutated copy.
        imports.discard("mutmut")
    return imports - STANDARD


def test_the_module_imports_only_the_standard_library() -> None:
    path = Path(receiver.__file__)
    assert foreign_imports(path.read_text(), path) == set()


def test_the_import_check_flags_a_real_foreign_import() -> None:
    source = "import os\nimport httpx\nfrom cryptography import x\n"
    assert foreign_imports(source, Path("src/audit_receiver.py")) == {
        "httpx",
        "cryptography",
    }
    assert foreign_imports("import mutmut\n", Path("src/audit_receiver.py")) == {
        "mutmut"
    }
    assert foreign_imports("import mutmut\n", Path("mutants/src/r.py")) == set()
    assert foreign_imports("import httpx\n", Path("mutants/src/r.py")) == {"httpx"}


@pytest.mark.parametrize("is_line", [True, False])
def test_a_short_write_refuses_the_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, is_line: bool
) -> None:
    directory = fresh(tmp_path)
    real = os.write

    def short(descriptor: int, data: bytes) -> int:
        if data.startswith(b"{") is is_line:
            return real(descriptor, data[:1])
        return real(descriptor, data)

    monkeypatch.setattr(os, "write", short)
    assert run(directory, "append 1", record(GENESIS)) == (1, b"")


def test_a_failed_write_during_rotation_leaves_the_head_on_the_old_segment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = fresh(tmp_path)
    first = record(GENESIS, ', "pad": "' + "x" * 40 + '"')
    second = record(digest(first))
    size = len(first)
    run(directory, "append 1", first, segment=size)
    before = (directory / "head").read_text()
    real = os.write

    def broken(descriptor: int, data: bytes) -> int:
        if data == second:
            raise OSError
        return real(descriptor, data)

    monkeypatch.setattr(os, "write", broken)
    assert run(directory, "append 2", second, segment=size) == (1, b"")
    monkeypatch.undo()
    assert (directory / "head").read_text() == before
    assert run(directory, "head", segment=size)[0] == 0
    assert not (directory / "segment-2.jsonl").exists()
    assert (directory / "segment-1.jsonl").read_bytes() == first


@pytest.mark.parametrize("name", ["segment-1.jsonl", "head", "lock"])
def test_a_symbolic_link_in_the_directory_is_never_followed(
    tmp_path: Path, name: str
) -> None:
    directory = fresh(tmp_path)
    target = tmp_path / "outside"
    target.write_bytes(b"keep")
    (directory / name).unlink(missing_ok=True)
    (directory / name).symlink_to(target)
    assert run(directory, "append 1", record(GENESIS))[0] == 1
    assert target.read_bytes() == b"keep"


def test_a_head_that_is_not_a_regular_file_is_refused(tmp_path: Path) -> None:
    directory = fresh(tmp_path)
    (directory / "head").unlink()
    os.mkfifo(directory / "head")
    assert run(directory, "head") == (1, b"")


def test_files_are_made_private_even_when_they_began_open(tmp_path: Path) -> None:
    directory = fresh(tmp_path)
    (directory / "segment-1.jsonl").chmod(0o644)
    (directory / "head").chmod(0o644)
    (directory / "lock").write_bytes(b"")
    (directory / "lock").chmod(0o666)
    first = record(GENESIS, ', "pad": "' + "x" * 40 + '"')
    second = record(digest(first))
    size = len(first)
    run(directory, "append 1", first, segment=size)
    run(directory, "append 2", second, segment=size)
    for name in ("segment-1.jsonl", "segment-2.jsonl", "head", "lock"):
        assert (directory / name).stat().st_mode & 0o777 == 0o600


def test_a_file_that_cannot_be_made_private_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = fresh(tmp_path)

    def denied(_descriptor: int, _mode: int) -> None:
        raise PermissionError

    monkeypatch.setattr(os, "fchmod", denied)
    assert run(directory, "head") == (1, b"")

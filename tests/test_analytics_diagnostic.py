"""Only whitelisted phrases and flags can escape migration diagnostics."""

import pytest

from tbhbdev.analytics.decisions.diagnostic import diagnostics, sentinels


@pytest.mark.parametrize(
    ("line", "label"),
    [
        ("PERMISSION DENIED fatal error", "permission denied "),
        ("Connection Refused syntax error", "connection refused "),
        ("SYNTAX ERROR fatal", "syntax error "),
        ("Fatal error", "FATAL "),
        ("ERROR", "ERROR "),
        ("private arbitrary text", ""),
        ("", ""),
    ],
)
def test_phrase_priority_and_case(line: str, label: str) -> None:
    assert diagnostics(line) == (f"migration diagnostic: {label}[redacted output]",)


@pytest.mark.parametrize(
    ("line", "flags"),
    [
        ("/example", " [path]"),
        ("service:123", " [endpoint]"),
        ("service:0", " [endpoint]"),
        ("service:x", ""),
        ("PASSWORD", " [credential]"),
        ("SECRET", " [credential]"),
        ("token", " [credential]"),
        ("credential", " [credential]"),
        ("authorization", " [credential]"),
        ("key", " [credential]"),
        ("Error /private:5432 password", " [path] [endpoint] [credential]"),
    ],
)
def test_diagnostic_flags_never_include_original_values(line: str, flags: str) -> None:
    label = "ERROR " if line.startswith("Error") else ""
    assert diagnostics(line) == (
        f"migration diagnostic: {label}[redacted output]{flags}",
    )


def test_only_last_five_lines() -> None:
    assert diagnostics(
        "permission denied\nconnection refused\nsyntax error\nfatal\nerror\nordinary\n"
    ) == tuple(
        f"migration diagnostic: {label}[redacted output]"
        for label in ("connection refused ", "syntax error ", "FATAL ", "ERROR ", "")
    )


def test_command_substitution_discards_trailing_blank_lines() -> None:
    assert diagnostics("error\n\n") == (
        "migration diagnostic: ERROR [redacted output]",
    )


def test_blank_lines_count_toward_last_five_before_trimming() -> None:
    assert diagnostics("permission denied\nerror\n\n\n\n\n\n") == (
        "migration diagnostic: [redacted output]",
    )


@pytest.mark.parametrize(
    ("output", "passwords", "expected"),
    [
        ("aa bb", ("aa", "bb", "cc"), ("aa", "bb")),
        ("AA", ("aa",), ()),
        ("a.b", ("a.b", "a*b"), ("a.b",)),
        ("none", (), ()),
        ("", ("", "x"), ("",)),
        ("repeat", ("repeat", "repeat"), ("repeat", "repeat")),
    ],
)
def test_sentinels_use_literal_case_sensitive_matches(
    output: str, passwords: tuple[str, ...], expected: tuple[str, ...]
) -> None:
    assert sentinels(output, iter(passwords)) == expected


def test_only_newlines_separate_log_records() -> None:
    assert diagnostics("permission denied\rfatal\vsyntax error") == (
        "migration diagnostic: permission denied [redacted output]",
    )


def test_trailing_whitespace_line_is_a_record() -> None:
    assert diagnostics("error\n \t\n") == (
        "migration diagnostic: ERROR [redacted output]",
        "migration diagnostic: [redacted output]",
    )


def test_trailing_nonblank_record_cannot_disappear() -> None:
    assert diagnostics("error\nX") == (
        "migration diagnostic: ERROR [redacted output]",
        "migration diagnostic: [redacted output]",
    )

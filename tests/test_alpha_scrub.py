# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns
"""Tests for tbhbdev alpha scrub: credential shapes go, ordinary text stays."""

import pytest

from tbhbdev.alpha import scrub

MARK = scrub.PLACEHOLDER
# Pieces are joined at run time so no literal here looks like a credential to
# the secret scanner.
BEGIN = "-----BEGIN "
END = "-----END "
KEY = "PRIVATE KEY-----"
BEARER = "Bearer" + " "
JWT = "ey" + "JhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ4In0.c2lnbmF0dXJl"
SERVICE = "OP_SERVICE_" + "ACCOUNT_TOKEN"
API = "api" + "_key"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            f"key {BEGIN}{KEY}\nabc\n{END}{KEY} end",
            f"key {MARK} end",
        ),
        (f"{BEGIN}RSA {KEY}\nabc", MARK),
        ("t ghp_" + "a" * 30 + " x", f"t {MARK} x"),
        ("github_pat_" + "B1" * 15, MARK),
        ("sk-ant-" + "c" * 25, MARK),
        ("ops_" + "d" * 30, MARK),
        ("AKIA" + "E" * 16, MARK),
        ("xoxb-" + "1" * 12, MARK),
        (JWT, MARK),
        (f"Authorization: {BEARER}" + "f" * 20, f"Authorization: {BEARER}{MARK}"),
        ("basic " + "g" * 16, f"basic {MARK}"),
        (
            "https://user:hunter2hunter2@example.com/x",
            f"https://user:{MARK}@example.com/x",
        ),
        (f"{SERVICE}=abcdefghij", f"{SERVICE}={MARK}"),
        ('password: "two words here"', f"password: {MARK}"),
        (f"{API} = 'abcdefgh1234'", f"{API} = {MARK}"),
        ("my_secret:abcdefgh", f"my_secret:{MARK}"),
    ],
)
def test_credentials_are_redacted(text: str, expected: str) -> None:
    assert scrub.scrub(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "commit " + "a" * 40,
        "token: short",
        "sk-short",
        "ghp_short",
        "plain prose, with a password policy",
        "https://example.com/path@x",
        "AKIA" + "E" * 15,
        "",
    ],
)
def test_ordinary_text_stays(text: str) -> None:
    assert scrub.scrub(text) == text


def test_a_secret_that_is_not_the_first_match_is_still_redacted() -> None:
    text = "ghp_" + "a" * 30 + " and ops_" + "b" * 30
    assert scrub.scrub(text) == f"{MARK} and {MARK}"

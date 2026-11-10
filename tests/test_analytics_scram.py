"""Fixed PostgreSQL verifiers and the RFC 7677 authentication exchange."""

import base64
import hashlib
import hmac

import pytest

from tbhbdev.analytics.decisions.scram import stable_salt, verifier


# cspell:ignore shcrr Jjbb GWDDs Euzyznk Gzfj Pnsqs Sldk TXALU Afux biws
# Ran perl scram.pl for each role, with password reads supplied from scalars:
# owner = "A" x 32; loader = "B" x 32; reader = "C" x 32;
# grafana_reader = "D" x 32. The original Perl computed each complete verifier.
@pytest.mark.parametrize(
    ("role", "password", "expected"),
    [
        (
            "loader",
            "B" * 32,
            (
                "SCRAM-SHA-256$4096:+4shcrrRQvAJjbbK+W5N2Q==$GWDDsRzRWHnSGNrHMfUghVM8"
                "u5keXrHfy6i7xzOKt2U=:70zDsCBlYyEuzyznkYISUguGzfjUhj+lUXplKlMB368="
            ),
        ),
        (
            "reader",
            "C" * 32,
            (
                "SCRAM-SHA-256$4096:Ts9bEg3G980j7uQB2S12Sg==$KWXosZ2ScStf0+uxiBPnsqsK"
                "Bh4E4i/wmfKbKJpUVrU=:Z8vwe2FOdcvWjFUMk/uab/JD0tVv5y1kOySKk0rCFZE="
            ),
        ),
        (
            "grafana_reader",
            "D" * 32,
            (
                "SCRAM-SHA-256$4096:vrpSldkHaBQa4rQ9OCYJpw==$y2k5k4TXALUEvScNILJxeYik"
                "IRPl81EBj9oZmz5D+7U=:HHuf1qnP/LVTsN2nv0ffObD4N+ItdthPmz6wrZGtsx0="
            ),
        ),
    ],
)
def test_perl_vectors(role: str, password: str, expected: str) -> None:
    salt = stable_salt("A" * 32, role)
    assert len(salt) == 16
    assert verifier(password, salt) == expected
    assert verifier(password, stable_salt("A" * 32, role), 4096) == expected


def test_owner_rotation_changes_salt() -> None:
    assert stable_salt("A" * 32, "loader") != stable_salt("E" * 32, "loader")


def test_rfc_7677_exchange() -> None:
    # https://www.rfc-editor.org/rfc/rfc7677.html#section-3
    salt = base64.b64decode("W22ZaJ0SNY7soEsUEjb6gQ==")
    value = verifier("pencil", salt)
    scheme, encoded = value.split("$", 1)
    parameters, keys = encoded.split("$")
    assert scheme == "SCRAM-SHA-256"
    assert parameters == "4096:W22ZaJ0SNY7soEsUEjb6gQ=="
    stored, server = map(base64.b64decode, keys.split(":"))
    nonce = "rOprNGfwEbeRWgbNEkqO%hvYDpWUa2RaTCAfuxFIlj)hNlF$k0"
    auth = (
        f"n=user,r=rOprNGfwEbeRWgbNEkqO,r={nonce},"
        f"s=W22ZaJ0SNY7soEsUEjb6gQ==,i=4096,c=biws,r={nonce}"
    ).encode()
    signature = hmac.digest(stored, auth, "sha256")
    proof = base64.b64decode("dHzbZapWIk4jUhN+Ute9ytag9zjfMHgsqmmiz7AndVQ=")
    client = bytes(a ^ b for a, b in zip(proof, signature, strict=True))
    assert hashlib.sha256(client).digest() == stored
    assert (
        base64.b64encode(hmac.digest(server, auth, "sha256"))
        == b"6rriTRBi23WpRR/wtup+mMhUZUn/dB5nLTJRsjl95G4="
    )


@pytest.mark.parametrize("iterations", [1, 2, 8192])
def test_explicit_iteration_count(iterations: int) -> None:
    value = verifier("pencil", b"salt", iterations)
    assert value.startswith(f"SCRAM-SHA-256${iterations}:c2FsdA==$")
    assert value != verifier("pencil", b"salt", 4096)


@pytest.mark.parametrize("iterations", [0, -1])
def test_non_positive_iterations(iterations: int) -> None:
    with pytest.raises(ValueError, match="iteration value must be greater than 0"):
        verifier("password", b"salt", iterations)

"""PostgreSQL SCRAM-SHA-256 verifiers and stable deployment salts."""

import base64
import hashlib
import hmac

_ALGORITHM = "sha256"


def stable_salt(owner_password: str, role: str) -> bytes:
    return hashlib.sha256(f"{owner_password}:{role}".encode()).digest()[:16]


def verifier(password: str, salt: bytes, iterations: int = 4096) -> str:
    salted = hashlib.pbkdf2_hmac(_ALGORITHM, password.encode(), salt, iterations)
    client = hmac.digest(salted, b"Client Key", _ALGORITHM)
    stored = hashlib.sha256(client).digest()
    server = hmac.digest(salted, b"Server Key", _ALGORITHM)
    encoded = [base64.b64encode(value).decode() for value in (salt, stored, server)]
    return f"SCRAM-SHA-256${iterations}:{encoded[0]}${encoded[1]}:{encoded[2]}"

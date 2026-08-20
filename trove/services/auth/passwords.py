"""Password hashing — stdlib only.

Django-style PBKDF2 encoding so the salt travels inside the hash string:

    pbkdf2_sha256$260000$<salt_b64>$<digest_b64>

No bcrypt/argon2 dependency keeps uv.lock untouched; 260k iterations is
the OWASP-recommended PBKDF2-HMAC-SHA256 cost at this tier.
"""

from __future__ import annotations

import base64
import hashlib
import secrets

HASH_ALGO = "pbkdf2_sha256"
HASH_ITERATIONS = 260_000
SALT_BYTES = 16


def hash_password(password: str) -> str:
    """Hash a password into a self-contained encoded string."""
    salt = secrets.token_bytes(SALT_BYTES)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, HASH_ITERATIONS)
    salt_b64 = base64.b64encode(salt).decode("ascii")
    digest_b64 = base64.b64encode(digest).decode("ascii")
    return f"{HASH_ALGO}${HASH_ITERATIONS}${salt_b64}${digest_b64}"


def verify_password(password: str, encoded: str) -> bool:
    """Check a plaintext password against an encoded hash string."""
    try:
        algo, iterations, salt_b64, digest_b64 = encoded.split("$", 3)
        if algo != HASH_ALGO:
            return False
        salt = base64.b64decode(salt_b64.encode("ascii"))
        expected = base64.b64decode(digest_b64.encode("ascii"))
    except (ValueError, TypeError):
        return False
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, int(iterations))
    return secrets.compare_digest(digest, expected)

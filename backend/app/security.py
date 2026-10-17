"""Password hashing and session tokens.

Deliberately stdlib-only. `hashlib.scrypt` is a memory-hard KDF that ships with
CPython, so the usual `passlib`/`bcrypt`/`argon2` dependency chain buys nothing
here beyond another wheel to audit.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets

# ~16 MiB and ~50 ms per hash on a laptop. High enough to make offline guessing
# expensive, low enough that a login is not noticeably slow.
_N = 2**14
_R = 8
_P = 1
_DKLEN = 32
_SALT_BYTES = 16

_ALGORITHM = "scrypt"


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii")


def hash_password(password: str) -> str:
    """Return a self-describing hash: `scrypt$n$r$p$salt$hash`."""
    salt = secrets.token_bytes(_SALT_BYTES)
    derived = hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=_N, r=_R, p=_P, dklen=_DKLEN
    )
    return f"{_ALGORITHM}${_N}${_R}${_P}${_b64(salt)}${_b64(derived)}"


def verify_password(password: str, encoded: str) -> bool:
    """Constant-time check of a password against a stored hash."""
    try:
        algorithm, n, r, p, salt_b64, hash_b64 = encoded.split("$")
        if algorithm != _ALGORITHM:
            return False
        derived = hashlib.scrypt(
            password.encode("utf-8"),
            salt=base64.urlsafe_b64decode(salt_b64),
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=len(base64.urlsafe_b64decode(hash_b64)),
        )
    except (ValueError, TypeError):
        return False

    return hmac.compare_digest(derived, base64.urlsafe_b64decode(hash_b64))


def new_session_token() -> str:
    """The value that goes in the cookie. Only ever seen by the browser."""
    return secrets.token_urlsafe(32)


# A recovery code is written down by hand and read back off the paper, so the
# letters that get misread are not in it: no O/0, no I/1/L, no B/8, no S/5/Z/2.
_CODE_ALPHABET = "ACDEFGHJKMNPQRTUVWXY34679"
_CODE_GROUPS = 5
_CODE_GROUP_SIZE = 4


def new_recovery_code() -> str:
    """A code a reader can copy onto paper without a mistake.

    Five groups of four from a 25-character alphabet is about 93 bits — the
    same order as the password it stands in for, and short enough that writing
    it down is not a chore. The dashes are for reading; they carry no entropy.
    """
    return "-".join(
        "".join(secrets.choice(_CODE_ALPHABET) for _ in range(_CODE_GROUP_SIZE))
        for _ in range(_CODE_GROUPS)
    )


def normalise_recovery_code(raw: str) -> str:
    """Strip what a reader may reasonably add or leave out: case, spaces, dashes.

    Both the storing and the checking side go through this, so a code is only
    ever hashed in the shape it will be typed in.
    """
    return "".join(character for character in raw.upper() if character.isalnum())


def token_fingerprint(token: str) -> str:
    """What the database stores instead of the token itself.

    A stolen database copy then yields no usable sessions. Plain SHA-256 is
    right here where scrypt is right for passwords: the input is 256 bits of
    entropy, so there is nothing to brute-force.
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()

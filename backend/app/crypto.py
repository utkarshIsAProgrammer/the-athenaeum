"""Reversible encryption for the one secret we have to be able to read back.

A password is hashed, because nothing ever needs it again. A provider key is
the opposite: it is replayed upstream verbatim, so it has to be stored in a
form the app can open. It is encrypted with a key derived from the deployment's
`secret_key`, which means a stolen `news.db` on its own is not enough to spend
anyone's credits.
"""

from __future__ import annotations

import base64

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

# HKDF wants a salt for domain separation, not per-message freshness — Fernet
# already handles the latter. Fixed is right, and it must never change: doing so
# would make every stored key unreadable.
_SALT = b"athenaeum.provider-keys.v1"
_INFO = b"provider-key-encryption"
_KEY_BYTES = 32


class SecretsUnavailable(RuntimeError):
    """No `secret_key` is configured, or the ciphertext will not open."""


def _fernet(secret: str) -> Fernet:
    if not secret:
        raise SecretsUnavailable(
            "Set SECRET_KEY in backend/.env — a provider key cannot be stored without it."
        )
    derived = HKDF(
        algorithm=hashes.SHA256(),
        length=_KEY_BYTES,
        salt=_SALT,
        info=_INFO,
    ).derive(secret.encode("utf-8"))
    return Fernet(base64.urlsafe_b64encode(derived))


def encrypt(secret: str, plaintext: str) -> str:
    return _fernet(secret).encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt(secret: str, ciphertext: str) -> str:
    try:
        return _fernet(secret).decrypt(ciphertext.encode("ascii")).decode("utf-8")
    except InvalidToken as exc:
        # Almost always a rotated secret. The reader simply adds the key again.
        raise SecretsUnavailable("That stored key can no longer be read.") from exc

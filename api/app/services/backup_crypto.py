"""Authenticated encryption for database backups.

The dump never leaves the host unencrypted: `scripts/backup.py` encrypts it
before writing the final file, and `scripts/restore.py` decrypts it into a
short-lived temporary file. Compression is provided by `pg_dump -Fc`.

File layout::

    b"DLPBAK1\\n" | 16-byte salt | Fernet token

The Fernet key is derived from the operator passphrase with PBKDF2-HMAC-SHA256
so a weak passphrase is still slow to attack. This does not replace keeping the
passphrase in a password manager.
"""

from __future__ import annotations

import base64
import os
from pathlib import Path

MAGIC = b"DLPBAK1\n"
SALT_LENGTH = 16
MIN_PASSPHRASE_LENGTH = 12
PBKDF2_ITERATIONS = 390_000


class BackupCryptoError(Exception):
    """Raised when a backup cannot be decrypted or is malformed."""


def _fernet(passphrase: str, salt: bytes):
    from cryptography.fernet import Fernet
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

    if len(passphrase) < MIN_PASSPHRASE_LENGTH:
        raise ValueError(
            f"Backup passphrase must be at least {MIN_PASSPHRASE_LENGTH} characters"
        )
    derived = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=PBKDF2_ITERATIONS,
    ).derive(passphrase.encode("utf-8"))
    return Fernet(base64.urlsafe_b64encode(derived))


def encrypt_bytes(data: bytes, passphrase: str) -> bytes:
    salt = os.urandom(SALT_LENGTH)
    token = _fernet(passphrase, salt).encrypt(data)
    return MAGIC + salt + token


def decrypt_bytes(blob: bytes, passphrase: str) -> bytes:
    from cryptography.fernet import InvalidToken

    if not blob.startswith(MAGIC):
        raise BackupCryptoError("Not a deliverability backup file (bad magic header)")
    payload = blob[len(MAGIC) :]
    if len(payload) <= SALT_LENGTH:
        raise BackupCryptoError("Backup file is truncated")
    salt, token = payload[:SALT_LENGTH], payload[SALT_LENGTH:]
    try:
        return _fernet(passphrase, salt).decrypt(token)
    except InvalidToken as exc:
        raise BackupCryptoError(
            "Backup could not be decrypted: wrong passphrase or corrupted file"
        ) from exc


def encrypt_file(source: Path, destination: Path, passphrase: str) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(encrypt_bytes(source.read_bytes(), passphrase))


def decrypt_file(source: Path, destination: Path, passphrase: str) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(decrypt_bytes(source.read_bytes(), passphrase))
